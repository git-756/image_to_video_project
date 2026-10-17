# image-to-video-project

フォルダごとの写真を、4K のスライドショー動画（mp4）にする GUI アプリです。

## 必要なもの

- macOS（Apple シリコン）。動画の書き出しに Mac のハードウェアエンコーダ（`hevc_videotoolbox`）を使います
- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- ffmpeg（`brew install ffmpeg`）

## 準備

```sh
uv sync
```

## 起動

```sh
uv run python src/main.py
```

## 使い方

1. 「フォルダを選択」を押し、**親フォルダ**を選ぶ
2. 「動画作成スタート！」を押す
3. 進み具合はプログレスバーとログ欄に出ます。終わると「完了」のダイアログが出ます

## フォルダの形

選ぶのは、写真が直接入ったフォルダではなく、その 1 つ上の親フォルダです。サブフォルダ 1 つにつき、動画が 1 本できます。

```text
旅行2026/            ← これを選ぶ
├── 1日目/           → 旅行2026_1日目.mp4
├── 2日目/           → 旅行2026_2日目.mp4
└── output_video/    ← 自動で作られ、動画と ffmpeg のログが入る
```

- 対象の画像: `.jpg` `.jpeg` `.png` `.heic` `.heif`
- 画像はファイル名の順に並びます
- 写真が 1 枚も無いサブフォルダは飛ばします
- `output_video/` 自体は処理の対象から外します

## できる動画

| 項目 | 内容 |
|---|---|
| サイズ | 3840×2160（4K）。縦長の写真などは、余白を黒で埋めます |
| 長さ | 1 枚につき 1 秒 |
| フレームレート | 2fps |
| 形式 | HEVC（H.265）、5Mbps、mp4 |
| 日時 | フォルダ内で一番古い写真の撮影日時（EXIF） |

動画の日時について:

- 動画の中の作成日時（写真アプリや QuickTime が使う日時）と、Finder の作成日・変更日の両方を、一番古い写真の撮影日時にそろえます
- EXIF に撮影日時が無いときは、写真ファイルの作成日時を使います

## うまくいかないとき

### ffmpeg が `Library not loaded` で起動しない

`brew upgrade` で、ffmpeg が使うライブラリ（libvpx や x265 など）だけが新しくなると起きます。ffmpeg を入れ直してください。

```sh
brew upgrade ffmpeg
# 直らなければ
brew reinstall ffmpeg
```

### 動画ができずに「エラー」と出る

`output_video/` の `<動画名>_ffmpeg_log.txt` に、ffmpeg が出したエラーが残っています。
