# VRChat YouTube Cache

YouTube動画を事前に取得し、VRChatで再生しやすい **H.264 / AAC のMP4** に変換してCloudflare R2へ保存する、個人用のキャッシュ管理ツールです。管理画面はCloudflare TunnelとCloudflare Accessで保護し、VRChatはR2の再生URLを直接読む構成です。

投稿者から許諾を得た動画など、利用権限のあるコンテンツだけを対象にしてください。YouTube・VRChat・Cloudflareの利用規約や、視聴地域・年齢・著作権上の制限を回避する用途には使えません。

## 構成と設計

```text
管理者のブラウザ / Quest Browser
        │ Cloudflare Access で認証
        ▼
cache.example.com ─ Cloudflare Tunnel ─ Flask / gunicorn (worker 1)
                                          │
                         yt-dlp → ffmpeg → rclone
                                          │
                                          ▼
                           R2: videos/<YouTube ID>.mp4
                                          │ 公開R2カスタムドメイン
                                          ▼
                   video.example.com/videos/<YouTube ID>.mp4
                                          │
                                      VRChat / VideoTXL
```

- **管理面と配信面を分離**します。`cache.example.com` はCloudflare Accessで保護し、R2用の `video.example.com` はVRChatが認証なしで読める公開URLです。
- **URLは厳格に検証**します。通常の `youtube.com` / `youtu.be` URLから11文字の動画IDだけを取り出し、yt-dlpには組み立て直した `youtube.com/watch?v=<ID>` だけを渡します。任意URLを取得させません。
- **R2を真実の情報源**にします。状態APIとリダイレクトは毎回R2を確認するため、サービス再起動後でもアップロード済み動画を `done` として扱えます。メモリ上のジョブ情報は処理中表示の補助です。
- **ジョブは1本ずつ**実行します。プロセス内キューの上限も設け、同じ動画IDはロック下で1ジョブに統合します。gunicornを複数ワーカーにするとメモリキューが分裂するため、必ず `--workers 1` にします。
- **失敗や中断後も一時ファイルを消去**します。動画長、変換後サイズ、空きディスク容量、待ち行列数を環境変数で制限できます。
- **`+faststart` MP4**を生成し、映像は H.264 / yuv420p、音声は AAC に正規化します。これでも個々のVRChatプレイヤー・端末での再生可否を保証するものではないため、実機テストは必要です。
- **出力フレームレートは30fps固定（CFR）**です。ffmpegの `fps=30` フィルタと出力 `-r 30` により、60fps・可変フレームレートを含む入力でも30fpsにフレームを間引き／複製します。GOPも60フレーム（30fpsで2秒）に固定します。

### API

| API | 動作 |
|---|---|
| `GET /` | 管理WebUI |
| `POST /api/cache` | YouTube URLを検証し、R2にあればURLを即返却。なければキュー投入 |
| `GET /api/status/<video_id>` | R2を優先確認して `queued` / `running` / `done` / `error` を返却 |
| `GET /<video_id>` | R2上のMP4へ302リダイレクト |

## 使用技術

| 役割 | 技術 |
|---|---|
| Webアプリ | Python 3、Flask、gunicorn |
| 取得 | yt-dlp |
| 変換 | ffmpeg（libx264 / AAC） |
| R2操作 | rclone（S3互換API） |
| 公開 | Cloudflare Tunnel、Cloudflare Access、R2 Custom Domain |

## 構築手順（Ubuntu 24.04）

以下ではアプリを `/opt/ytcache`、設定を `/etc/ytcache`、実行ユーザーを `ytcache` とします。実行ユーザーを固定すると、R2資格情報や作業ファイルの所有者が曖昧になりません。

### 1. OSパッケージと実行ユーザー

```bash
sudo apt update
sudo apt install -y python3-venv ffmpeg curl
curl https://rclone.org/install.sh | sudo bash
sudo useradd --system --create-home --home-dir /var/lib/ytcache --shell /usr/sbin/nologin ytcache
sudo install -d -o ytcache -g ytcache -m 0700 /var/tmp/ytcache /etc/ytcache
sudo install -d -o ytcache -g ytcache -m 0755 /opt/ytcache
```

`rclone` のインストールスクリプトを使わない運用では、公式パッケージ等で導入して `/usr/local/bin/rclone` または設定した絶対パスに配置してください。

### 2. ファイルの配置とPython環境

このディレクトリの内容を `/opt/ytcache` に配置し、実行ユーザーに読み取り権限を与えます。

```bash
sudo cp -a . /opt/ytcache/
sudo chown -R root:ytcache /opt/ytcache
sudo chmod -R g+rX /opt/ytcache
sudo python3 -m venv /opt/ytcache/venv
sudo /opt/ytcache/venv/bin/pip install -r /opt/ytcache/requirements.txt
sudo install -m 0640 -o root -g ytcache /opt/ytcache/.env.example /etc/ytcache/ytcache.env
sudoedit /etc/ytcache/ytcache.env
```

`PUBLIC_BASE_URL` と `R2_REMOTE` を必ず実値へ変更してください。値に空白を含めないでください。`MAX_DURATION_SECONDS`、`MAX_OUTPUT_BYTES`、`MIN_FREE_BYTES` はサーバー容量に合わせて調整します。

### 3. R2とrclone

R2にバケットを作り、`Object Read & Write` 権限を持つ**そのバケットだけ**のAPIトークンを発行します。`/etc/ytcache/rclone.conf` を作成します。

```ini
[r2]
type = s3
provider = Cloudflare
access_key_id = <ACCESS_KEY_ID>
secret_access_key = <SECRET_ACCESS_KEY>
endpoint = https://<ACCOUNT_ID>.r2.cloudflarestorage.com
region = auto
```

```bash
sudo chown root:ytcache /etc/ytcache/rclone.conf
sudo chmod 0640 /etc/ytcache/rclone.conf
sudoedit /etc/ytcache/ytcache.env  # RCLONE_CONFIG=/etc/ytcache/rclone.conf を追加
sudo -u ytcache RCLONE_CONFIG=/etc/ytcache/rclone.conf rclone lsf r2:mybucket
```

R2バケットに `video.example.com` の**カスタムドメイン**を接続します。`r2.dev` のPublic Development URLは有効にしないでください。カスタムドメインを公開すると、そのURLを知る第三者も原則ファイルへアクセスできます。公開して問題のない動画だけを置き、必要ならCloudflareのWAFトークン認証などを検討してください（VRChatがその方式を使えるかは事前検証が必要です）。

アップロード後は、次を必ず確認します。

```bash
curl -I https://video.example.com/videos/<VIDEO_ID>.mp4
curl -H 'Range: bytes=0-1023' -I https://video.example.com/videos/<VIDEO_ID>.mp4
```

`Content-Type: video/mp4` とRangeレスポンスを確認し、実際のQuest/VideoTXLで再生・シークテストをしてください。

### 4. systemdサービス

`/etc/systemd/system/ytcache.service` を作成します。

```ini
[Unit]
Description=VRChat YouTube Cache controller
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=ytcache
Group=ytcache
WorkingDirectory=/opt/ytcache
EnvironmentFile=/etc/ytcache/ytcache.env
ExecStart=/opt/ytcache/venv/bin/gunicorn --workers 1 --threads 4 --bind 127.0.0.1:8080 --access-logfile - --error-logfile - app:app
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/tmp/ytcache

[Install]
WantedBy=multi-user.target
```

`PATH` は環境ファイルの例のように、venvだけでなく `/usr/local/bin` と `/usr/bin` も含めてください。含めないと rclone や ffmpeg を起動できません。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ytcache
sudo systemctl status ytcache
```

### 5. Cloudflare Tunnel とAccess

Cloudflare Dashboardで `cache.example.com` をこのサーバーの `http://127.0.0.1:8080` に向けるTunnelとして作成します。ダッシュボードに表示されるトークン方式のインストール手順を使うのが簡単です。

ローカル管理Tunnelを使う場合、`sudo cloudflared service install` はrootのホームディレクトリを見に行きます。ユーザーの設定を使うなら、明示的に `sudo cloudflared --config /home/<user>/.cloudflared/config.yml service install` としてください。

Cloudflare Accessで `cache.example.com` 全体を対象にSelf-hosted applicationを作成し、許可ユーザーだけのAllowポリシーを設定します。
`/api/cache` だけではなく、管理UI・状態API・リダイレクトすべてを保護します。さらにWAFで `POST /api/cache` のレート制限を設定します。R2の `video.example.com` はVRChat用に別ホストとして扱い、Access管理画面の保護対象に混ぜません。

## 利用方法

1. Access認証済みのブラウザで `https://cache.example.com/` を開く。
2. 許諾済み動画のYouTube URLを入力して実行する。
3. 処理完了後に出る `https://video.example.com/videos/<VIDEO_ID>.mp4` をコピーする。
4. VRChatのVideoTXL等にそのMP4 URLを入力する。
5. 既に存在する動画は `https://cache.example.com/<VIDEO_ID>` へのアクセスでもR2 URLへリダイレクトされる。ただしVRChatに渡すURLは、Accessの影響を避けるため原則としてR2のURLを使う。

## 運用と障害対応

- ログ: `journalctl -u ytcache -f`。Cloudflare Tunnelは `journalctl -u cloudflared -f`。
- サービス再起動中のジョブは中断されます。中間ファイルは削除され、R2に完了済みのファイルだけが次回以降キャッシュ済みとして検出されます。
- R2保存済み動画は自動削除されません。R2 Lifecycle Ruleでプレフィックス `videos/` に保存期間を設定するか、許可済みの運用スクリプトで整理してください。
- `MAX_PENDING` を超える要求はHTTP 429で拒否されます。通常は `MAX_WORKERS=1`、`MAX_PENDING=3` を維持するのが安全です。
- yt-dlpの仕様変更、年齢制限、地域制限、ログイン必須動画などでは取得に失敗することがあります。エラーはUIとjournalに表示されます。Cookie・アカウント資格情報をこの公開管理ツールに持ち込む場合は、リスクを理解した上で別途厳格に管理してください。
- 更新時はファイル配置後に `sudo systemctl restart ytcache`。アップデート前後に短尺動画でR2のContent-Type、Range、Quest再生を確認してください。
- **30fps化の反映:** 既にR2に保存済みの動画は、同じ動画IDで再実行しても再変換されません。30fps版を作るには対象オブジェクトだけを削除してから、管理UIで再度キャッシュしてください。例: `sudo -u ytcache RCLONE_CONFIG=/etc/ytcache/rclone.conf rclone deletefile r2:mybucket/videos/<VIDEO_ID>.mp4`。削除対象とバケット名を確認してから実行してください。

## 制約

- 処理中・待機中の状態は単一プロセスのメモリにあり、永続キューではありません。R2の存在確認で完了状態だけは復元できます。
- この実装は配信ファイルへのアクセス制御を提供しません。VRChat互換の公開MP4と、アクセス制御の強さにはトレードオフがあります。
- 複数台・複数gunicornワーカーへの水平拡張は未対応です。必要になった場合はRedis等を使う永続ジョブキューと分散ロックへ置き換えてください。
