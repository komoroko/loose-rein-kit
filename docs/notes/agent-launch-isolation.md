# エージェント CLI 自体の隔離

**状態**: 実装済み(`executors.agent_profile` / `kind: oci-agent`)。既定は無効。

## 何が問題だったか

`executors.for_profile` を通る経路は2つしかなかった。

- `build_loop._run_cmd_step` — 品質ゲートの `kind: command` ステップと、同じランナーを通る
  受入基準の `command`
- `audit.run` — 依存監査。これは逆向きで、サンドボックスプロファイルを**拒否**する
  (公開脆弱性データベースを読むが、どのプロファイルにも egress が無かったため)

エージェント CLI —— 実装者・タスクごとのレビュアー・review fixer・conflict fixer・
integration fixer —— は、いずれも `common.run` でホストプロセスとして起動していた。cwd は
リポジトリか git worktree で、資格情報は利用者のものそのままである。品質ゲートをサンドボックス化
しても、**そのコードを書いた工程はホストで全権限のまま動いていた**。

加えて `executors.implementer_profile` と `reviewer_profile` は、schema が required にしていながら
コンテナ起動経路に一切到達しない設定だった。設定した人に、存在しない境界を信じさせていた。

## どう直したか

**2つの kind に分けた。** ネットワークに対する要求が正反対だからである。

| kind | 何を包むか | network |
|---|---|---|
| `oci` | リポジトリ由来コードの実行(品質ゲート・受入基準の command) | `none` 固定。他の値は拒否 |
| `oci-agent` | エージェント CLI 自身 | `egress` 必須。`none` は拒否 |

1つの kind にノブを1つ足す形にしなかったのは、その形だと**設定ミス1つで品質ゲートに出口が
生える**からである。executor は kind で判定し、caller の申告では判定しない。

`executors.agent_profile` にそのプロファイルを指すと、`build_loop._launch` を通る全エージェント
起動がその中で走る。worktree は `/work` に read-write でマウントされ(`mount_repo` が何と
言おうと read-write —— 書けないエージェントは起動する意味がない)、制御ソケットは
`/run/rein/control.sock` に bind される。ホスト側のソケットは `/run/user/<uid>/rein/<id>/` に
あり、そのディレクトリごとマウントすると他のソケットまで渡ることになるので、固定パスに張り直す。
`REIN_CONTROL_SOCKET` はコンテナ内のパスに書き換えられ、それ以外の `REIN_*` はそのまま渡る。

**制御ソケットのやりとりは `env_allowlist` の対象外**にした(`ExecutionSpec.env_always`)。
allowlist はホストの環境変数が漏れ込むのを止めるためのもので、これらはホストの環境変数ではなく
この起動のために mint されたものである。allowlist に書き忘れたら、リーフが自分の成果を報告
できないのに理由がどこにも出ない —— という形の失敗になる。

## この箱が買うもの・買わないもの

買うのは、**コードを書くプロセスが利用者のマシンの他の部分を読めない**ことである。HOME も
`~/.ssh` も `~/.aws` も docker socket も capability も無く、マウントされた worktree の外に
出る経路が無い。

買わないのは**情報持ち出しに対する防御**である。リポジトリはマウントされ、ネットワークは
egress 開放(engine 既定の bridge、フィルタ無し)である。モデル API に到達できないエージェントは
何もできない以上、ここを閉じる選択肢は無い。`executors.py` も schema も README も、この点を
「フィルタしている」とは書いていない。

## 既定が無効である理由

イメージが CLI を内包する必要があり、同梱イメージが任意の CLI を網羅することはできない。
だから `AGENT_CLI` は build 引数であり、だから `agent_profile` は optional である。設定しない
ことは正当な選択で、その場合は `rein doctor` が WARN を出し、dossier の `REIN_SANDBOX` と
acceptance の brief が「ホストで動いた」と述べる。**黙って host に落ちる経路は無い**:
`agent_profile` が `kind: host` を指していれば `rein build` は起動せずに落ちる。

## 検証

`tests/test_control_plane.py::test_a_container_can_reach_the_control_plane_over_the_bound_socket`
(`integration` マーカー、docker が無ければ skip)が、実際のコンテナから実際の制御プレーンへ
実際のプロトコルで `knowledge_gap.create` を投げ、ホスト側の `events.ndjson` に着地することを
確認する。`--user 1000:1000` / `--cap-drop ALL` / `--read-only` / `--network none` を全部
掛けた状態で bind した unix socket が通るかは argv からは読めないので、ここだけは実機で確かめる。

**未検証**: 実際のエージェント CLI を入れたイメージのビルドと、その中からのモデル API 呼び出し。
`npm install -g` と TLS 到達が要るため、この環境では確認していない。
