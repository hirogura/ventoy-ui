# ventoy-ui v0.0.4

CachyOS (デスクトップ環境なし) で Webブラウザから Ventoy USB を作成するための Web-UI。
GUI相当の設定をブラウザから行える。

- 公開ポート: `3363`
- Ventoy ダウンロード先: `/opt/ventoy` (ボタンで作成・展開)
- 対象: USBドライブ (プルダウン) / イメージファイル (`ventoy.qcow2` 等の任意パス)
- オプション: GPT/MBR (既定 GPT)、インストール (`-i`) / 強制インストール (`-I`) / アップデート (`-u`)、
  Secure Boot (`-s`/`-S`)、予約領域 (`-r`)、ラベル (`-L`)、非破壊インストール (`-n`)
- 管理: GitHubからアップデート (`git pull`)、Ventoy-UIの再起動

## ISOイメージダウンロード (管理カードの上)

- USBメモリ / qcow2・img イメージ内の Ventoyデータパーティション (第2パーティション) を
  `/mnt/ventoy-iso` にマウントし、保存済みISOの一覧表示とアンマウントが可能
- URL入力欄に `.iso` の直接リンクを入力してダウンロード (cachy-UI の Limine編集
  「または直接URLを入力」と同じ流儀: URL検証・進捗バー・キャンセル付き)
- qcow2 のマウントには `qemu-img` が必要 (`sudo pacman -S qemu-img`)。raw (.img) は `losetup` で対応
- qcow2 マウント時は nbdモジュールのロード・空き `/dev/nbdN` の自動確保を行う
  (モジュールが無い環境では理由付きのエラーを表示)

## 使い方

```bash
# 直接起動
sudo python3 /opt/ventoy-ui/app.py
# http://<host>:3363 にアクセス

# systemd で常駐 (推奨)
sudo cp /opt/ventoy-ui/ventoy-ui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ventoy-ui
```

## 依存関係

- Python3 (標準ライブラリのみ)
- `lsblk`, `git`, `curl` 相当機能 (ダウンロードは Python 内蔵)
- qcow2 新規作成時のみ `qemu-img` があれば使用 (`sudo pacman -S qemu-img`)、無ければ raw で作成

## 注意

- 書き込みには root 権限が必要。USB 書き込み時は対象デバイスを必ず確認すること。
- Ventoy 本体の CLI 仕様: `Ventoy2Disk.sh {-i|-I|-u} [-g] [-s/-S] [-r MB] [-L label] [-n] /dev/sdX`
