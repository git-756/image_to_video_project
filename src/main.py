import os
import sys
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timezone
from PIL import Image, ExifTags, ImageOps

# HEICサポート用のライブラリをインポートし、Pillowに登録
import pillow_heif
pillow_heif.register_heif_opener()

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QProgressBar, QTextEdit, QFileDialog, QMessageBox
)
from PySide6.QtCore import QThread, Signal

# 画像の4K化を並列で行うスレッド数（Pillowは処理中にGILを解放するのでスレッドで並列になる）
MAX_WORKERS = os.cpu_count() or 4


def prepare_image(img_path, temp_img_path):
    """1枚の画像を3840x2160に統一してJPEGで保存する"""
    with Image.open(img_path) as img:
        # 縦写真が横に寝ないようにEXIFの回転情報を適用（HEICも対応）
        img = ImageOps.exif_transpose(img)
        # 3840x2160にリサイズ＆余白を黒で塗りつぶし
        img_padded = ImageOps.pad(img, (3840, 2160), color=(0, 0, 0))
        # エラー防止のためRGBモードに統一
        if img_padded.mode != 'RGB':
            img_padded = img_padded.convert('RGB')
        img_padded.save(temp_img_path, "JPEG", quality=95)
    return temp_img_path


def get_oldest_creation_date(image_paths):
    """画像群から最も古い撮影日時（EXIF）を取得する"""
    oldest_date = None
    for path in image_paths:
        try:
            with Image.open(path) as img:
                # HEIC等での互換性を高めるため、プライベートメソッド _getexif() から getexif() に変更
                # 撮影日時は getexif() の直下ではなく Exif IFD の中にあるので、そこから読む
                exif_ifd = img.getexif().get_ifd(ExifTags.IFD.Exif)
                value = (exif_ifd.get(ExifTags.Base.DateTimeOriginal)
                         or exif_ifd.get(ExifTags.Base.DateTimeDigitized))
                if value:
                    dt = datetime.strptime(value.strip("\x00 "), "%Y:%m:%d %H:%M:%S")
                    if oldest_date is None or dt < oldest_date:
                        oldest_date = dt
        except Exception:
            pass
            
    if oldest_date is None and image_paths:
        try:
            birth_times = [os.stat(p).st_birthtime for p in image_paths]
            oldest_date = datetime.fromtimestamp(min(birth_times))
        except AttributeError:
            # OSや環境によって st_birthtime が無い場合のフォールバック
            m_times = [os.path.getmtime(p) for p in image_paths]
            oldest_date = datetime.fromtimestamp(min(m_times))
        
    return oldest_date


class VideoWorker(QThread):
    """動画生成を裏で行うためのワーカースレッド"""
    progress_updated = Signal(int)
    log_updated = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, parent_dir):
        super().__init__()
        self.parent_dir = Path(parent_dir)

    def run(self):
        try:
            output_dir = self.parent_dir / "output_video"

            # 出力先フォルダ自体は動画化の対象から外す
            subfolders = [
                p for p in self.parent_dir.iterdir()
                if p.is_dir() and p != output_dir
            ]
            total_folders = len(subfolders)

            if total_folders == 0:
                self.log_updated.emit("サブフォルダが見つかりません。")
                return

            output_dir.mkdir(parents=True, exist_ok=True)
            parent_name = self.parent_dir.name

            for i, subfolder_path in enumerate(subfolders):
                subfolder_name = subfolder_path.name
                # 対象拡張子に .heic と .heif を追加
                image_paths = sorted([
                    p for p in subfolder_path.iterdir() 
                    if p.is_file() and p.suffix.lower() in ['.jpg', '.jpeg', '.png', '.heic', '.heif']
                ])
                
                if not image_paths:
                    self.log_updated.emit(f"スキップ: {subfolder_name} (画像なし)")
                    self.progress_updated.emit(int(((i + 1) / total_folders) * 100))
                    continue

                self.log_updated.emit(f"処理中: {subfolder_name} ({len(image_paths)}枚)")

                oldest_date = get_oldest_creation_date(image_paths)
                
                if oldest_date:
                    local_tz = datetime.now().astimezone().tzinfo
                    dt_aware = oldest_date.replace(tzinfo=local_tz)
                    dt_utc = dt_aware.astimezone(timezone.utc)
                    metadata_time = dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
                else:
                    metadata_time = ""

                self.log_updated.emit("すべての画像を4Kサイズに最適化中...")

                # 一時フォルダを作成して、画像をすべて同じサイズ(3840x2160)に統一する
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_dir_path = Path(temp_dir)
                    temp_image_paths = []

                    # 全画像を並列で変換し、結果は元の並び順で受け取る
                    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                        futures = [
                            executor.submit(prepare_image, img_path, temp_dir_path / f"img_{idx:04d}.jpg")
                            for idx, img_path in enumerate(image_paths)
                        ]
                        for img_path, future in zip(image_paths, futures):
                            try:
                                temp_image_paths.append(future.result())
                            except Exception as e:
                                self.log_updated.emit(f"画像読み込みエラー ({img_path.name}): {e}")

                    if not temp_image_paths:
                        self.log_updated.emit("有効な画像がありませんでした。スキップします。")
                        continue

                    # 元の画像の代わりに、サイズ統一済みの一時画像を使ってリストを作成
                    concat_file_path = subfolder_path / "ffmpeg_concat_list.txt"
                    with open(concat_file_path, "w", encoding="utf-8") as f:
                        for img_path in temp_image_paths:
                            f.write(f"file '{img_path.resolve()}'\n")
                            f.write("duration 1.0\n") # 1枚につき1秒
                        f.write(f"file '{temp_image_paths[-1].resolve()}'\n")

                    output_filename = f"{parent_name}_{subfolder_name}.mp4"
                    output_filepath = output_dir / output_filename
                    log_file_path = output_dir / f"{output_filename}_ffmpeg_log.txt"

                    # 画像はすでに4K化されているので、FFmpeg側でのリサイズはせずピクセルフォーマットと2fpsのみ指定
                    # （1枚1秒の静止画なので2fpsで十分）
                    vf_filter = "format=yuv420p,fps=2"
                    
                    cmd = [
                        "ffmpeg", "-y",
                        "-f", "concat",
                        "-safe", "0",
                        "-i", str(concat_file_path),
                        "-vf", vf_filter,
                        "-c:v", "hevc_videotoolbox", 
                        "-b:v", "5M",
                        "-tag:v", "hvc1",
                        "-pix_fmt", "yuv420p",
                    ]
                    
                    if metadata_time:
                        cmd.extend(["-metadata", f"creation_time={metadata_time}"])
                        
                    cmd.append(str(output_filepath))

                    try:
                        result = subprocess.run(
                            cmd, 
                            stdout=subprocess.PIPE, 
                            stderr=subprocess.PIPE, 
                            text=True
                        )
                        
                        with open(log_file_path, "w", encoding="utf-8") as lf:
                            lf.write(f"--- 実行したコマンド ---\n{' '.join(cmd)}\n\n")
                            lf.write(f"--- FFmpeg ログ ---\n{result.stderr}")

                        if result.returncode == 0:
                            if oldest_date:
                                # Finderの作成日・変更日も撮影日時にそろえる（APFSでは変更日を戻すと作成日も戻る）
                                ts = oldest_date.timestamp()
                                os.utime(output_filepath, (ts, ts))
                            self.log_updated.emit(f"完了: {output_filename}")
                            if "Drop" in result.stderr or "drop" in result.stderr or "Error" in result.stderr:
                                self.log_updated.emit(f"⚠️ 警告: ログにDropやErrorの記載があります。")
                        else:
                            self.log_updated.emit(f"エラー: {subfolder_name} の処理に失敗しました。")
                            
                        self.log_updated.emit(f"詳細はログを確認してください: {log_file_path.name}")

                    except Exception as e:
                        self.log_updated.emit(f"実行エラー: {str(e)}")
                    finally:
                        if concat_file_path.exists():
                            concat_file_path.unlink()

                self.progress_updated.emit(int(((i + 1) / total_folders) * 100))

            self.log_updated.emit(f"\nすべての処理が完了しました！\n出力先: {output_dir}")

        except Exception as e:
            self.error_occurred.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Image to 4K Video Converter - Mac mini M4 Edition")
        self.resize(600, 400)

        self.selected_folder = ""

        self.folder_label = QLabel("対象の親フォルダを選択してください")
        self.select_btn = QPushButton("フォルダを選択")
        self.select_btn.clicked.connect(self.select_folder)

        self.start_btn = QPushButton("動画作成スタート！")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_processing)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)

        top_layout = QHBoxLayout()
        top_layout.addWidget(self.folder_label)
        top_layout.addWidget(self.select_btn)

        main_layout = QVBoxLayout()
        main_layout.addLayout(top_layout)
        main_layout.addWidget(self.start_btn)
        main_layout.addWidget(self.progress_bar)
        main_layout.addWidget(self.log_output)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def select_folder(self):
        folder_path = QFileDialog.getExistingDirectory(self, "親フォルダを選択")
        if folder_path:
            self.selected_folder = folder_path
            self.folder_label.setText(f"選択中: {folder_path}")
            self.start_btn.setEnabled(True)
            self.log_output.append(f"フォルダが選択されました: {folder_path}")

    def start_processing(self):
        self.start_btn.setEnabled(False)
        self.select_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log_output.append("--- 処理を開始します ---")

        self.worker = VideoWorker(self.selected_folder)
        self.worker.progress_updated.connect(self.progress_bar.setValue)
        self.worker.log_updated.connect(self.log_output.append)
        self.worker.finished.connect(self.processing_finished) 
        self.worker.error_occurred.connect(self.show_error)
        self.worker.start()

    def processing_finished(self):
        self.start_btn.setEnabled(True)
        self.select_btn.setEnabled(True)
        self.progress_bar.setValue(100)
        QMessageBox.information(self, "完了", "すべての動画の作成が完了しました！")

    def show_error(self, error_msg):
        self.start_btn.setEnabled(True)
        self.select_btn.setEnabled(True)
        QMessageBox.critical(self, "エラー発生", f"予期せぬエラーが発生しました:\n{error_msg}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())