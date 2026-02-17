import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime
from PIL import Image, ExifTags

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QProgressBar, QTextEdit, QFileDialog, QMessageBox
)
from PySide6.QtCore import QThread, Signal

def get_oldest_creation_date(image_paths):
    """画像群から最も古い撮影日時（EXIF）を取得する"""
    oldest_date = None
    for path in image_paths:
        try:
            with Image.open(path) as img:
                exif = img._getexif()
                if exif:
                    for tag_id, value in exif.items():
                        tag = ExifTags.TAGS.get(tag_id, tag_id)
                        if tag == 'DateTimeOriginal':
                            dt = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
                            if oldest_date is None or dt < oldest_date:
                                oldest_date = dt
        except Exception:
            pass
            
    if oldest_date is None and image_paths:
        birth_times = [os.stat(p).st_birthtime for p in image_paths]
        oldest_date = datetime.fromtimestamp(min(birth_times))
        
    return oldest_date


class VideoWorker(QThread):
    """動画生成を裏で行うためのワーカースレッド"""
    progress_updated = Signal(int)
    log_updated = Signal(str)
    finished = Signal()
    error_occurred = Signal(str)

    def __init__(self, parent_dir):
        super().__init__()
        self.parent_dir = Path(parent_dir)

    def run(self):
        try:
            subfolders = [p for p in self.parent_dir.iterdir() if p.is_dir()]
            total_folders = len(subfolders)

            if total_folders == 0:
                self.log_updated.emit("サブフォルダが見つかりません。")
                self.finished.emit()
                return

            output_dir = self.parent_dir.parent / "OutputVideos"
            output_dir.mkdir(parents=True, exist_ok=True)
            parent_name = self.parent_dir.name

            for i, subfolder_path in enumerate(subfolders):
                subfolder_name = subfolder_path.name
                image_paths = sorted([
                    p for p in subfolder_path.iterdir() 
                    if p.is_file() and p.suffix.lower() in ['.jpg', '.jpeg', '.png']
                ])
                
                if not image_paths:
                    self.log_updated.emit(f"スキップ: {subfolder_name} (画像なし)")
                    self.progress_updated.emit(int(((i + 1) / total_folders) * 100))
                    continue

                self.log_updated.emit(f"処理中: {subfolder_name} ({len(image_paths)}枚)")

                oldest_date = get_oldest_creation_date(image_paths)
                metadata_time = oldest_date.strftime("%Y-%m-%dT%H:%M:%S.000000Z") if oldest_date else ""

                concat_file_path = subfolder_path / "ffmpeg_concat_list.txt"
                with open(concat_file_path, "w", encoding="utf-8") as f:
                    for img_path in image_paths:
                        f.write(f"file '{img_path.resolve()}'\n")
                        f.write("duration 0.5\n")
                    # concatの仕様上、最後の画像を記述（durationなし）
                    f.write(f"file '{image_paths[-1].resolve()}'\n")

                output_filename = f"{parent_name}_{subfolder_name}.mp4"
                output_filepath = output_dir / output_filename

                # 【修正1】fpsを10に変更（容量削減）
                vf_filter = "fps=10,scale=3840:2160:force_original_aspect_ratio=decrease,pad=3840:2160:(ow-iw)/2:(oh-ih)/2:color=black"
                
                cmd = [
                    "ffmpeg", "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_file_path),
                    "-vf", vf_filter,
                    # 【修正2】H.265 (HEVC) のハードウェアエンコーダーを指定
                    "-c:v", "hevc_videotoolbox", 
                    # 【修正3】ビットレートを20Mから5Mに大幅ダウン（H.265かつ静止画メインならこれで十分綺麗です）
                    "-b:v", "5M",
                    # 【追加】AppleのQuickTime等でH.265として正しく認識させるためのタグ
                    "-tag:v", "hvc1",
                    "-pix_fmt", "yuv420p",
                ]
                
                if metadata_time:
                    cmd.extend(["-metadata", f"creation_time={metadata_time}"])
                    
                cmd.append(str(output_filepath))

                try:
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.log_updated.emit(f"完了: {output_filename}")
                except subprocess.CalledProcessError:
                    self.log_updated.emit(f"エラー: {subfolder_name} の処理に失敗しました。")
                finally:
                    if concat_file_path.exists():
                        concat_file_path.unlink()

                # 進捗バーの更新
                self.progress_updated.emit(int(((i + 1) / total_folders) * 100))

            self.log_updated.emit(f"\nすべての処理が完了しました！\n出力先: {output_dir}")
            self.finished.emit()

        except Exception as e:
            self.error_occurred.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Image to 4K Video Converter - Mac mini M4 Edition")
        self.resize(600, 400)

        self.selected_folder = ""

        # UI要素の作成
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

        # レイアウトの組み立て
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

        # ワーカースレッドの準備と開始
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