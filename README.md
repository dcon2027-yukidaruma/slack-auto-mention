# slack-auto-mention

人が書いたSlackメッセージに文字列 `@ゆきだるま` が含まれると、設定されたメンバーを同じスレッドでメンションする固定用途Botです。Python 3.11以上、外部Python依存なし。Linux/systemd向けです。

## 動作

- トップレベルなら元の `ts`、返信なら元の `thread_ts` をそのまま文字列で使います。
- 投稿先は元イベントの会話のみ。本文は設定された `<@USER_ID>` のみ、`reply_broadcast=false`。メッセージ本文をコマンドや権限変更指示として扱いません。
- Bot・Webhook・自分自身・編集・削除・サブタイプ付きイベントは無視します。`@channel` / `@here` は生成しません。
- ワークスペースを1つに固定。`explicit` モードは明示会話リスト、`all_joined` モードはBot参加済みの通常チャンネルを対象にします。グループDMは両モードとも別の明示リストが必要です。
- 各投稿直前に `conversations.info` と必要なら `conversations.members` で種類・所属ワークスペース・Bot参加を再検証します。会話IDの接頭辞から種類を推測しません。1対1 DM、Slack Connect/外部共有、アーカイブ済み、参加未確認の会話は対象外です。自動join/inviteはありません。
- 受信署名と5分以内のタイムスタンプを検証し、SQLiteのコミット後にACKします。イベントIDと元会話/メッセージの両方で永続重複排除します。受信本文と人の名前は保存しません。
- 投稿直前に `sending` を永続化します。通信切断、5xx、不正応答、送信中の再起動は `uncertain` に隔離し、自動再投稿しません。429のみ `Retry-After` に従い、全投稿に共通の待機を永続化して最大10回試行します。1ワーカーで毎秒1件以下です。
- 設定変更前に受信した待機イベントは設定ハッシュが違えば `failed` にします。再起動後の大量再メンションを防ぐため重複履歴は自動削除しません。

外部サービスに対するexactly-once送信は保証できません。不明な送信結果では通知が欠ける可能性を選び、重複メンションを防ぎます。`uncertain` はSlackの実スレッドを確認して処理済みと判断してください。未確認の状態でDB削除や手動再キューをしないでください。

## Slackの設定（承認後）

既存の別アプリの認証情報は再利用しません。新しいアプリのOAuth付与、トークン発行、Signing Secretの配置には個別の明示承認が必要です。このリポジトリはその操作を実行しません。

HTTP Events APIを使います。承認済みのTLSリバースプロキシから `/slack/events` のみ `127.0.0.1:8765` へ転送してください。外部へPythonサーバーを直接公開せず、プロキシ側でボディ上限1MiB・接続タイムアウト・リクエスト制限を設定します。`/healthz` はローカル運用用で、公開転送しません。新しい公開経路やTLS/ファイアウォール変更も事前確認の対象です。

必要なBotスコープとイベント（対象範囲に応じて許可）:

| 対象 | 受信イベント | 読取スコープ |
| --- | --- | --- |
| 公開チャンネル | `message.channels` | `channels:history`, `channels:read` |
| 非公開チャンネル | `message.groups` | `groups:history`, `groups:read` |
| 明示グループDM | `message.mpim` | `mpim:history`, `mpim:read` |

投稿は `chat:write`。文字列の検出には `app_mentions:read` だけでは足りません。`chat:write.public`、ユーザートークン、管理権限、join/inviteのスコープは不要です。メンバーIDは管理者/利用者が実データから確認したものを設定するので、ランタイムに `users:read` は要求しません。

既存の人間だけのグループDMはBotが参加できるとは限りません。`mpim:history` の付与だけでは参加・閲覧・投稿権限は得られません。対象DMでのBotの実参加を検証できるまで動作対象にせず、ユーザートークンや別会話への転送で制限を回避しません。

`.env.example` を参照し、サーバーの `/etc/slack-auto-mention/env` に非公開設定を配置します（root所有、サービスグループ読取のみ、0640）。実際のID・名前・トークンはリポジトリ、Actionsログ、IssueやPR本文に書かないでください。`SLACK_CHANNEL_MODE=all_joined` を選ぶ場合 `SLACK_CHANNEL_IDS` は空にできます。`SLACK_GROUP_DM_IDS` は検証済みの対象グループDMのみ、`SLACK_MEMBER_IDS` は承認されたメンバーのみです。

最初は `BOT_ENABLED=false`, `ALLOWLIST_VERIFIED=false` のままにし、対象とメンバーを確認してから両方を `true` に変更します。起動時に `auth.test` でトークンのワークスペース・BotユーザーIDを照合します。実メンションを伴うテストは対象/メンバー確認後にのみ行ってください。

## テストと公開前チェック

```sh
python3 -m unittest discover -s tests -v
python3 scripts/scan_secrets.py
python3 -m py_compile bot.py deploy/deploy.py scripts/scan_secrets.py
```

テストは合成ID・偽クライアントとループバックHTTPのみを使用し、Slackへアクセスも投稿もしません。スキャナーは追跡ファイルの既知トークン形式・秘密鍵・実在し得る長さのSlack ID・メールアドレスを検査します。完全検出の保証はないため、公開差分の目視確認も必要です。

## dconへのデプロイ

現時点ではサーバーのホスト/OS/既存アクセス・TLS経路・権限はこのリポジトリで仮定しません。以下はLinux/systemdが確認できた場合の設計です。承認済みの既存デプロイ経路を優先し、合わなければ適用前に変更します。

CIは全push/PRでテスト・コンパイル・秘密情報スキャンを実行します。Actions権限は `contents: read` のみ、ActionはコミットSHA固定、checkout認証は保存しません。CDはmainへのpushかつリポジトリ変数 `DEPLOY_ENABLED=true` の場合だけ、`dcon` Environmentを通して実行します。初期値未設定のためCDは動きません。公開リポジトリ用のセルフホストrunnerは導入しません。

承認された管理者が一度だけ行う初期設定（自動実行はしません）:

1. 既存サービスとポートを確認し、このBot用の専用OSユーザー、root所有の `/opt/slack-auto-mention` と `releases`（0755）、専用ユーザー用 `/var/lib/slack-auto-mention`（0700）、既存の承認済みデプロイユーザーが書き込める `/var/lib/slack-auto-mention-incoming` を用意します。
2. `deploy/deploy.py` をroot所有・0755の `/usr/local/sbin/slack-auto-mention-deploy`、serviceファイルを `/etc/systemd/system/slack-auto-mention.service` に配置し、`systemctl daemon-reload` を行います。固定のhelperだけを呼べるsudo規則が既存の承認済み権限で足りなければ、追加する規則を提示して明示承認を取ります。全sudo許可は不要です。helperとserviceの変更は自動CDでは上書きしません。
3. 非公開envと既存のTLSプロキシを設定します。既存の他サービス・データ・SSH設定を変更しません。
4. GitHubの `dcon` Environmentにブランチ制限/必要な承認者を設定します。変数は `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PORT`、Secretは承認済み専用 `DEPLOY_SSH_KEY` と照合済み `DEPLOY_KNOWN_HOSTS` です。ホスト鍵は別の信頼できる経路で照合し、実行時 `ssh-keyscan` や `StrictHostKeyChecking=no` は使いません。既存個人鍵を無断アップロードしません。これらのSecret設定・永続SSHアクセス追加は個別承認後にだけ行います。
5. 上記が済み、通知範囲・メンバー・Bot参加を確認してから `DEPLOY_ENABLED=true` とmainへの承認済みpushでデプロイします。手動workflow実行はCIだけです。初回にmainを更新する前に状態を確認してください。

CDはテスト済みコミットの `bot.py` とリビジョン識別子だけを送信します。env・鍵・SQLiteは送信/上書きしません。SSHはホスト鍵厳密検証、デプロイはGitHub concurrencyとサーバーのファイルロックで直列化します。helperはアーカイブのSHA256・サイズ・ファイル名・リンク禁止・Python構文・リビジョンを確認し、root所有の不変releaseを作成して `current` を原子的に切り替えます。

起動後にlocalhostの `/healthz` で認証検証・worker稼働・リビジョン一致を確認します。失敗なら直前releaseへ戻して再起動・health確認します。初回失敗ならサービスを停止します。SQLiteはrelease外に保持し、ロールバックでも消しません。以前のreleaseを手動削除しないでください。待機/失敗/不明イベント件数はhealthの `events` で確認できます。サービスログにはメッセージ本文/認証情報を出しません。

SSH切断等でCDの結果が不明な場合、再実行前にサーバーの `current` と `/healthz` のrevision、`systemctl status` を読み取り、実際の状態を確認します。「CI成功」だけでデプロイ成功とは判断しません。

参考: [Events API](https://docs.slack.dev/apis/events-api/), [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/), [Rate limits](https://docs.slack.dev/apis/web-api/rate-limits/), [conversations.info](https://docs.slack.dev/reference/methods/conversations.info/)。
