# 未解決: エージェント CLI 自体の隔離

**状態**: 未着手。次サイクルの mandate に載せる候補。

## 事実

`executors.for_profile` を通る経路は2つしかない。

- `build_loop._run_cmd_step` — 品質ゲートの `kind: command` ステップと、同じランナーを通る
  受入基準の `command`
- `audit.run` — 依存監査。これは逆向きで、サンドボックスプロファイルを**拒否**する
  (公開脆弱性データベースを読むが、どのプロファイルにも egress が無いため)

エージェント CLI —— 実装者・タスクごとのレビュアー・review fixer・conflict fixer・
integration fixer・gate ④ の3ステージ —— は、いずれも `common.run` でホストプロセスとして
起動する。cwd はリポジトリか git worktree で、資格情報は利用者のものそのままである。

## この回で直したこと

`executors.implementer_profile` と `reviewer_profile` は、schema が required にしていながら
コンテナ起動経路に一切到達しない設定だった。用途は (a) 未サンドボックス警告の判定、
(b) preflight のイメージ存在確認、(c) dossier の `env.sandbox` への文字列申告 の3つだけで、
(c) に至ってはエージェントに真でない環境を申告していた。この3つとも削除し、同梱の
`implementer` / `reviewer` Containerfile も削除した。方針文(AGENTS.md・config.schema.json・
`executors.py`・README 2本)は、実装が保証できる範囲 ——「リポジトリ由来コードの*実行*は
OCI、エージェント CLI 自身はホスト」—— に書き直した。

## 残っている問い

実装者エージェントは、テストファイルを書く前から任意のコマンドを実行できる。品質ゲートを
サンドボックス化しても、そのコードを書いた工程はホストで全権限のまま動いている。

今日この境界を引く手段は1つだけある。`rein` 自体をコンテナ内で動かし、アダプタ側の
サンドボックスを無効化することである(入れ子のサンドボックスはエージェントが書き込む地点で
失敗し、`doctor.running_containerized` がその組み合わせを警告する)。これは `rein` の外側の
運用であって、`executor_profiles` の設定項目ではない。

設定項目として提供するなら、`build_loop._launch` を `executors` 経由に通す必要がある。
その際に決める必要があるのが以下である。

- エージェント CLI 自体をイメージに入れるのか、ホストのバイナリをマウントするのか
- モデル API への egress をどう扱うか(現在 `network_profile` は `none` 以外を拒否する)
- 資格情報をどう渡すか(`env_allowlist` は書かれているが、上の拒否のため誰も使っていない)
- worktree の read-write マウントと control socket のバインド

3つ目が本丸である。egress なしにモデルを呼べない以上、「ネットワークは全面禁止」という
現在の不変条件そのものを作り直すことになる。
