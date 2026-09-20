# Loose Rein

[English](README.md) | **日本語**

**Human on the Loop** でソフトウェアを開発するためのコーディングエージェント用ハーネス。作業と
証拠の生成はエージェントが行い、人間はフェーズ境界 —— *ゲート* —— で承認する。

ハーネスの本体はインストール型の CLI (`rein`) である。プロダクトのリポジトリが持つのは状態だけで、
`.rein/`(SSOT・ロック・実体化されたプロンプトとスキーマ)と `docs/`(成果物)がそれにあたる。

このページは導入と運用の手順である。常に真であるルールは [`AGENTS.md`](AGENTS.md) にあり、verb の
一覧は `rein help --all`、各 verb の引数は `rein <verb> --help` が示す。

## しくみ

**人間が承認するのは2回。そのどちらも、作業の順序についての承認ではない。**

```mermaid
flowchart LR
    brief["brief<br/>(人間が構想を書く)"]:::human

    subgraph DRAFT["drafting — 順不同・反復可・承認は1回"]
        direction TB
        req["/req<br/>claims"]:::agent
        design["/design<br/>approach + ADRs"]:::agent
        tasks["/tasks<br/>scope + task DAG"]:::agent
    end

    g1{"mandate<br/>何を変えてよいか<br/>何を真にするか<br/>何が証拠になるか"}:::human
    build["/build<br/>実装ループ"]:::agent
    verify["/verify<br/>検証"]:::agent
    g2{"acceptance<br/>変更を受け入れる"}:::human
    done(["done"])

    subgraph TASKS["タスク DAG — mandate とともに凍結され、順序はループが決める"]
        direction TB
        T1["foundation T-001"]:::agent
        T2["leaf T-002"]:::agent
        T3["leaf T-003"]:::agent
        TI["integration T-0xx"]:::agent
        T1 --> T2
        T1 --> T3
        T2 --> TI
        T3 --> TI
    end

    brief --> DRAFT --> g1
    g1 -->|"並列 (最大3)"| build
    tasks -.->|generates| T1
    build --> verify --> g2 --> done

    RV(("/revise")):::human
    g2 -.-> RV
    RV -.-> g1

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#0b3a6f;
    classDef human fill:#86dfaa,stroke:#0f7a3d,color:#04301a;
    style DRAFT fill:#f1f6fe,stroke:#c9ddf7,color:#57606a;
    style TASKS fill:#f1f6fe,stroke:#c9ddf7,color:#57606a;
    style done fill:#ffffff,stroke:#9aa0a6,color:#26282b;
```

緑は人間が行う箇所 —— brief・ゲート・`/revise` である。青はエージェントが実行するもの。点線の矢印は
人間の裁量による差し戻しを表す。

| フェーズ | コマンド | 何が起きるか | 人間の役割 |
|---|---|---|---|
| drafting | `/req` `/design` `/tasks` | claim・方針・スコープ・タスク DAG —— 順不同 | **mandate** を承認する: スコープ・claim・受入基準 |
| 実装 | `/build` | mandate の内側での自律ループ | なし —— レビュー指摘は自分で修正する |
| 検証 | `/verify` | テスト・依存関係監査・grounded review | **acceptance** を承認する: 変更を受け入れる |

drafting の3コマンドは、3つで**1つの mandate** を書く。したがってそれ自体がゲートなのではなく
mandate の材料である —— 順序は変更に合わせて選んでよく、反復してもよい。自由なのは順序であって中身
ではない: `rein approve mandate` は claim を1つも述べない plan と、タスクを1つも宣言しない plan を
拒否するので、`/req` と `/tasks` はどの経路で到達したにせよ答えられることになる。省いてよいのは
`/design` で、そのときトレーサビリティは design の次元を「通った」ではなく**未検査**として報告する。
3つをまとめて承認する唯一の判断がこれであり、`plan.yaml` と `config.yaml` を凍結する。

承認された mandate の内側で、`/build` は mandate の `scope` の外のパスに触れられず(`rein guard` が
拒否する)、mandate が要求する証拠なしには完了できない。自由に選べるのは順序・並列度・赤になった
ステップの再実行である。できないのは DAG の切り直しで、各タスクの受入基準は凍結された `plan.yaml` の
中にあるからである。分割をやり直すなら `/revise --to mandate` を通る。

**3種類目のゲートは、変更がそれを持つときに現れる。** `operator_surface` が `reversible: false` を
宣言するタスク —— データが動く、バージョンが公開される、課金が発生する —— には、そのタスクの名前を
持つゲートが生まれ、`rein build` はその手前で止まって `rein approve T-NNN` を待つ。これらは mandate の
承認時に、その承認が凍結する plan から現れる。つまり回数も mandate の一部として承認することになる。
上限は設けていない: 何回聞かれるかは変更の性質であって、このツールの定数ではない。

## セットアップ

6手順。`rein doctor` はいつでも全項目を点検できるので、green になった時点で新しいエージェント
セッションを開き、`/req` から始めればよい。

**1. 前提** —— POSIX 環境と、サンドボックス用のコンテナランタイム(docker または podman)。Linux・
WSL・macOS に対応する。Windows native は**未検証**である: 起動を拒否こそしないが、ファイルロックは
`msvcrt` にフォールバックし、ディレクトリの `fsync` は省かれ、並列ビルドが使うコントロールプレーンは
Unix domain socket を前提にしている。WSL を使うこと。

**2. CLI を導入する** —— フックが PATH 上で解決できるようにする:

```bash
uv tool install 'git+https://github.com/komoroko/loose-rein-kit.git@vX.Y.Z'   # `rein` が入る
# vX.Y.Z は最新のリリースタグに差し替える: https://github.com/komoroko/loose-rein-kit/releases
```

**3. ヘッドレスのエージェント CLI を用意する** —— 実装フェーズが呼び出す対象である。既定は `claude`
で、`rein agent <cli>` により切り替える。起動できるのは7つあり、「何を指示できるか」が異なる
(現在の割り当ては `rein agent --show`、PATH 上の有無は `rein doctor` が示す):

| `adapter:` | 実行ファイル | model 指定 | リトライがセッションを継続 | 消費量の報告 |
|---|---|---|---|---|
| `claude` | `claude` | 可 | 可(読解の共有もフォークできる) | 可 |
| `codex` | `codex` | 可 | 可 | 可 |
| `gemini` | `gemini` | 可 | 不可 | 可 |
| `copilot` | `copilot` | 可 | 不可 | 不可 |
| `cursor` | `cursor-agent` | 可 | 不可 | 不可 |
| `amp` | `amp` | 不可 | 不可 | 不可 |
| `opencode` | `opencode` | 可 | 不可 | ステップが報告したときのみ |

この表から2つの制約が導かれる。

- model を指示できないアダプタは、隣に書かれた `model:` を**拒否する**。自分の既定モデルを別の名前で
  起動することはしない。acceptance ゲートの独立性判定は model から導かれるので、「書いただけで実行
  されていない分離」は書かれた場所で止める。
- 標準入力からプロンプトを読むのは `claude` と `codex` の2つで、他は引数として受け取る。引数1つの
  上限は OS 側で 128 KiB である。grounded review の reading は通常それを超えるので、他のアダプタでは
  レビュアーの起動を**拒否し**、逃げ道を2つ名指しする: レビュアー役を `claude` か `codex` に向けるか、
  `review_policy.budgets.max_diff_bytes` を下げるか。それらの CLI で実際に読めるのは 128 KiB 程度
  までの変更である。

実行ファイルが無ければ `rein build` は起動せず、それをインストールするコマンドを示す。

**4. リポジトリを初期化する** —— 新規でも既存でも同じコマンドを使う。既存かどうかは自動で判定される。

```bash
cd myrepo && git init

rein start   # ウィザード: プロダクト名・brief の1行・エージェント連携ファイル・サンドボックス
# 非対話で行う場合(何度実行しても安全):
#   rein init --name <product> [--branch build/<product>]
```

**5. エージェント連携ファイルを配置する** —— フェーズコマンド(`/req`・`/design` など)は、これが
書かれて初めて存在する。通常はウィザードが行うので、次は2つめのホストを足すときや、非対話で初期化した
リポジトリに入れるときに使う:

```bash
rein install claude         # .claude/ のラッパー + settings.json のマージ
rein install copilot        # .github/ の prompt / agent / hook ラッパー
rein install codex          # .agents/skills/ と .codex/ のラッパー
rein install gemini         # .gemini/ の commands / skills + settings.json のマージ
```

これらはセッションやエディタの起動時にのみ読み込まれることが多い。実行後は**新しい**セッションを
開くこと。

**6. サンドボックスイメージをビルドする** —— 品質ゲートの command ステップは、リポジトリのコードと
テストをホストではなくサンドボックス内で実行する。エージェントが書いたテストを利用者の資格情報つきで
実行させないためである。pin が完了するまで `rein doctor` は FAIL を報告する:

```bash
rein oci build --all --write-config   # docker か podman が必要
```

同梱イメージに入っているのは python・uv・pytest だけで、ネットワークも無い。同梱の既定ゲートを動かすには
足りるが、それ以上は動かない。リンタ・型検査・依存関係の解決を要するゲートには専用のイメージが要る:
Containerfile を書き、プロファイルの `containerfile:` を `dockerfile:` に置き換えて指し、ビルドし直す。
詳細は `.rein/config.yaml` の `SANDBOXES` ブロックに記載してある。

**エージェント CLI 自身を包む**のは、これとは別の任意の箱である。ネットワークに対する要求が正反対
なので、種類も別になっている:

```bash
rein oci build --profile agent --build-arg AGENT_CLI=@anthropic-ai/claude-code --write-config
```

以後、実装者・レビュアー・fixer はすべてその中で起動する。worktree は `/work` にマウントされ、制御
ソケットが bind され、そして**利用者の HOME も ~/.ssh も ~/.aws も docker socket も無い**。egress は
与えられる —— モデル API に到達できないエージェントは何もできない —— ので、これは情報持ち出しに対する
境界では**なく**、そう主張もしない。買えるのは「コードを書くプロセスが、マシンの他の部分を読めない」
ことである。有効にしないことも正当な選択で、既定はそちらである: イメージが CLI を内包する必要がある
以上、同梱イメージが任意の CLI を網羅することはできない。どちらで動いたかは `rein doctor`・dossier・
acceptance の brief が必ず述べる。箱に入れる場合はアダプタ側のサンドボックスを無効化すること —— 入れ子の
サンドボックスはエージェントが書き込む地点で失敗する。

## 使い方

日常的に使うのは次の3つで、それ以外はダッシュボードのボタンに相当する操作である。

```bash
rein start        # 初回: セットアップウィザード / 以降: 前回見たときから動いたもの
rein next         # 次に実行すべきコマンドだけ(連携用に --json)
rein ui           # ローカルダッシュボード。成果物を読み、その場で承認できる
```

日常的にではなく最初に一度設定するもの: `rein agent codex` はヘッドレスで使うエージェント CLI を
切り替え、`rein project add` はダッシュボードの切替対象にリポジトリを登録する。単発であれば
`rein --repo <path> <verb>` でディレクトリを移動せずに別のリポジトリを対象にできる。

1サイクルは次の手順で進む。

1. **brief を書く** —— `docs/00-product-brief.md` に「何を作りたいか」を数行。人間が書く出発点は
   これだけである。

2. **フェーズを実行する** —— 次のコマンドは `rein next` が示す。`/status` は同じ内容を、タスク DAG と
   ともにチャットへ出力する。

3. **ゲートを開く** —— これは人間の行為であって、エージェントの行為ではない。場所は2つあり、端末で
   `rein approve <gate>` を実行するか、成果物を読んだ `rein ui` の画面のまま承認する。どちらも事前に
   承認可能かを確認し、その承認が対象とする digest を表示する。

   ```bash
   rein approve acceptance
   #   gate 'acceptance' is ready. This approval will cover:
   #     plan_digest          sha256:…
   #     attested_chain_root  sha256:…
   #   Approve gate 'acceptance'? [y/N] y
   ```

4. **修正を求める** —— 成果物が適切でない場合の正規の選択肢である。プロンプトで no と答えるか、
   ダッシュボードの *Request changes* を使う:

   ```bash
   rein changes add requirements --target docs/10-requirements.md#R-3 \
                                 --reason "受入基準が計測不能"
   ```

   未対応の要求がある間、ゲートは閉じたままになる。要求は `state.yaml` に記録されるので、登録した
   セッションが終了しても失われない。`--target` で箇所を指定すると、エージェントはその部分だけを
   修正するので文書全体の再生成にはならない。応答は `rein changes address <id> --note <何を変えたか>`
   で行う。

5. **差し戻す** —— ゲートを承認した**あとで**上流の不備が見つかった場合は `/revise <phase>` を実行
   する。戻し先から下流のゲートが連鎖的に `pending` へ戻る。`rein revise --impacted T-00x` は、指定した
   起点タスクとその下流をまとめて `needs-revision` にする。自動で波及することはなく、基盤タスクを
   起点にすると下流のほぼ全体が含まれるため、起点は狭く選ぶとよい。

6. **進捗を確認する** —— `rein start` は冒頭に **Waiting on you** を置く。リポジトリと次のゲートの
   間にある課題を重大な順に並べ、それぞれを解消するコマンドを示す。この行は
   `rein approve <gate> --check` が拒否する理由そのものなので、開かないゲートについてボードが
   「対応不要」と表示することはない。`rein ui` は同じものをページにしたもので、このサイクルのゲートが
   左端の spine に並び、それぞれの背後に読み室があり、ほかに DAG・イベントログのライブ表示・診断が
   ある。ポーリングはせずストリームを1本張るだけで、操作は固定ホワイトリスト —— 読み取り・診断・
   意思決定の記録に限られ、フェーズ実行や push は行えない。ほかに `rein dag --mermaid` が依存図を
   生成し、`rein decisions` と `rein claims` がアーカイブを読み戻す —— 記録済みの判断すべてと、各
   サイクルが何を満たすと約束したかを、古いサイクルから順に。

7. **PR にする** —— `rein pr-draft` が SSOT から PR 本文を組み立て、`.rein/pr-draft.md` に出力する。
   PR の作成と push は人間が行う。1タスク1PR の**スタック**として出すこともできる:
   `rein pr-stack` は各タスクが着地したコミットで作業ブランチを切り分け、スライスごとに本文を書く。
   `--push` は端末で確認を取ってから draft として開き、`--ready` は acceptance の承認後に draft を
   外す。レビュー指摘の修正は、そのコードを入れたスライスにコミットし、`--restack` がマージで上へ
   伝播させる。**スタックを rebase してはならず、部分的にマージしてもならない** —— どちらも
   `completed_commit` とゲート受領証が存在しないコミットを指す結果になる。全体の着地は
   `gh stack merge <top> --merge` で行う。これには `gh extension install github/gh-stack` が要り、
   導入済みかどうかは `rein doctor` が答える。任意で `rein issue-sync` が plan のタスクを
   GitHub Issues へ一方向ミラーする(既定は off。Issues 側の編集は読み戻さない)。

8. **サイクルを閉じる** —— `rein cycle-close --name <slug>` が docs を `docs/archive/<日付>-<slug>/` へ
   アーカイブし、新しいスキャフォールドを復元し、ゲートとフェーズをリセットする。ゲートを開くのと
   同様、これも人間の操作である。

**順番が来たことを知る。** 作業が何回止まるかを決めるのはゲートだが、1回の停止がどれだけ続くかを
決めるのは「どれだけ早く気づくか」である。`rein ui` は起動している間 SSOT を監視し —— ブラウザは
不要 —— 人間を待っている判断が変わったときに指定のコマンドを実行する:

```yaml
# $XDG_CONFIG_HOME/rein/notify.yaml   (~/.config/rein/notify.yaml)
command: notify-send "rein"
```

コマンドは `REIN_PROJECT`・`REIN_DECISION_ID`・`REIN_HEADLINE`・`REIN_ACTION`・`REIN_URL` を環境に
持って実行される。土台は資格情報を含まない許可リストである。1つの判断につき通知は1つで、内容は
「何を待っているか」と「どこで答えるか」に限られる —— 証拠は載らず、起動シークレットも `REIN_URL` から
除去される。指し示すページは、そのブラウザが既にセッションを持っていなければ読み取り専用である。

## インストールを最新に保つ

- **`rein sync`** —— インストール済みパッケージからプロンプトとスキーマを再実体化する。未変更の
  ファイルは更新し、ローカルで変更したファイルは保持して一覧に表示する(`--force` で上書き、
  `--check` は書き込まずに差分を報告する)。
- **`rein upgrade`** —— CHANGELOG の差分を表示したうえで、ツールが実体化したものをすべて更新する。
- **`rein doctor`** —— ネットワークに触れる唯一のコマンドである。GitHub に新しいリリースの有無を
  問い合わせ、**このインストールを実際に前へ進めるコマンド**を表示する。タグ固定のインストールと
  ブランチ追従のインストールでは必要なコマンドが違うため、文面で固定せず導出している。`gh` が無い・
  オフライン・VCS 由来でないインストールの場合は、「最新である」ではなく「確認できなかった」と報告
  する。`REIN_NO_UPDATE_CHECK` を設定すると問い合わせ自体を行わない。`rein start` はこの結果を
  doctor のキャッシュから表示するだけで、自身でネットワークに取りに行くことはない。

## ゲートを開く権限

`.rein/state.yaml` の `gates.<name>` が `approved` になる経路は1つだけで、`rein` が記録した人間の
承認に限られる。実作業をエージェントが担当していてもこれが成立し続けるのは、次の仕組みによる。

- **記録経路は1つ、使う場所が2つ** —— 端末か、ダッシュボードの承認フッターか。receipt は対象と
  なった digest を束縛し、どちらの経路で確認したかを記録する(誰が承認したかは記録しない)。
- **事故や既定値や事前承認では起こらない。** `rein approve` は対話的な TTY を必要とするので、パイプ・
  CI ジョブ・エージェントのサブプロセスはいずれも失敗する。ダッシュボードは `rein ui` を起動した端末
  だけに表示される使い捨ての起動リンクを使う。そして `rein doctor` は、ゲートを開く verb を事前承認
  している設定ファイルが無いことを —— gitignore されたローカル設定も含めて —— 検査する。`--force` は
  存在しない。
- **エージェントは3段階で締め出される** —— `rein guard` フックが編集を拒否し、シェル経由の書き込みが
  そのマッチャを抜けた場合は commit 段階の `rein guard --check-diff` が捕まえ、CI の base 側
  `rein policy-check` がそのいずれかを弱めようとする PR を落とす。ゲート行の手編集は拒否され、フックを
  無効化するキーは受け付けられず、読めないゲートは閉じる側に倒れる。
- **承認を巻き戻すのも人間の権限である。** `/revise` は戻し先から下流のゲートを連鎖的にリセットし、
  その上に築かれた receipt とレビューを無効にする。自動で巻き戻ることはない。
- **待つことは遊ぶことではなく、待つ間に何をしたかは記録に残る。** ゲートが pending の間に進めてよい
  のは**結果に依存しない作業**だけである —— スキャフォールド・CI 整備・読み取りのみの調査・フィクスチャ。
  `guard.paths` の外で、既定では捨てる前提とし、`docs/speculative-work.md` に「何を前提にしたか」と
  ともに記録する。何が無駄になるかを名指しできない行は、投機的作業ではなく成果物そのものだった。

## 利用者が設定するリポジトリ設定

Loose Rein はこれらを読んで診断するが、設定はしない。自分を裁く検査を自分で付与できるツールは、
境界ではないからである。

| 設定 | 理由 |
|---|---|
| base ブランチを保護する(直接 push 禁止・PR 必須) | ハーネスが強制するゲート境界はすべて作業ブランチ上にあり、直接 push はその全部を迂回する |
| テストジョブと base 側 policy check を必須にする | `policy-check` は PR が偽装できない唯一の検査である —— 信頼できる base 側から head のツリーを読む。必須にしなければ助言に留まる |
| 新しいコミットで既存の承認を取り消す | 承認の対象は diff であって、ブランチ名ではない |
| `.rein/` やワークフローを変更する PR の自己承認を禁止する | それらは境界そのものである |
| CI でのシークレットスキャン | commit 段階のフックは、それを導入した開発者しか守らない |

`rein doctor` はローカルから見える範囲を報告する: `rein policy-check` を実行するワークフローがあるか、
そのイベントが head の選べない base を渡しているか、実行している `rein` がどこから来たか。ジョブ自体は
ここに書き下しておく。ステップの順序に意味があり、しかもそれが他の多くのジョブの順序とは違うためである:

```yaml
  policy-check:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: astral-sh/setup-uv@<commit sha>
      # チェックアウトより前に、かつ head が書いていないコミットから導入する。そうしないと、PR が
      # 自分の検証器の依存解決先を選べてしまう(依存は起動時に import される)。`--no-config` は、
      # 順序を入れ替えただけでそこが黙って再び開かないようにするためのもの。`uv tool install` は
      # bin ディレクトリを PATH に追加しないので自分で指定して追加する —— `runner` はジョブ階層の
      # `env:` からは読めないコンテキストなので、ステップに置く。
      - env:
          UV_TOOL_BIN_DIR: ${{ runner.temp }}/rein-bin
        run: |
          uv tool install --no-config \
            "git+https://github.com/komoroko/loose-rein-kit.git@<.rein/rein.lock のタグ>"
          echo "$UV_TOOL_BIN_DIR" >> "$GITHUB_PATH"
      - uses: actions/checkout@<commit sha>
        with:
          fetch-depth: 0
      - run: >-
          rein policy-check
          --base-sha '${{ github.event.pull_request.base.sha }}'
          --head-sha '${{ github.event.pull_request.head.sha }}'
          --base-ref '${{ github.event.pull_request.base.ref }}'
          --default-branch 'origin/${{ github.event.repository.default_branch }}'
```

古い形のままのジョブが遡って失敗することはない: base 側が報告するのは head が**持ち込んだ**もので
あり、既存のものを名指しするのは `rein doctor` の役目である。

## エージェントの自己申告ではなく証拠で判定する

エージェントによる自分の作業の説明は、構造上つねに自己整合的である。だからゲートの判定材料には
決してならない。判定材料になるのは次のものである。

- **証拠のない claim は `unknown` であって、散文ではない。** `plan.yaml` は要件1つにつき claim を1つ
  凍結する —— これが Expected Model である —— そして `claim_ids` が各タスクを、それが答える claim へ
  結びつける。整合は `rein dag --trace` が検査する。
- **grounded review は Expected と Actual を突き合わせる。** 決定論的な Coverage Manifest、コードが
  実際に何をしているかの**ブラインド抽出**、構造化されたセキュリティレビュー、そしてその2つの比較を
  実行する。ブラインド抽出器には plan を渡さない。読みに行けないようリポジトリの外で起動し、テストも
  読ませない —— テスト名は、それが見ていない要件を言い換えたものだからである。
- **変更は一度にではなく、複数の reading に分けて読む** —— スコープを持つタスクごとに1つ、加えて2つの
  スコープが共有する部分とどのスコープにも属さない部分の継ぎ目に1つ。すべての記述はそれがどの reading
  から出たかを持ち、どの reading も覆わなかった変更パスがあれば manifest は `insufficient` になる。
  `critical` のリスクでは、設定が何であれ変更を丸ごと読む。マージ後のツリーにも専用の reading がある
  (`stage: integration`)。重複や、1つの責務が2か所にあることなど、結合によって生じるものは、個々の
  スコープの内側からは誰にも見えないからである。
- **単一の `verified` は存在しない。** 指摘は3つの独立した軸 —— integrity・semantic support・
  conformance —— に並び、「余分な振る舞い: 0」はそれを裏づける manifest とともにしか現れない。blocking の
  セキュリティ指摘、high/critical の diverged な claim、根拠のない high/critical の余分な振る舞い、
  不十分な manifest のいずれかがあればゲートは開かない。後続のコミットはレビューを stale にする。
- **裁く者は修理しない。** レビュアーは読み取り専用で起動して指摘を書き、実装者がそれを解消し、
  レビュアーがもう一度読む(`review_policy.repair_rounds` まで)。実装者は
  `rein report --outcome implemented|blocked|needs-revision` で終える —— これは実際の diff と突き合わせ
  られる「主張」であって、判定ではない。
- **`done` は、そのタスクが実際に生み出したツリーに対して DoD が green になったことを意味する** ——
  状態の隣に内容のフィンガープリントが記録される。何も変えなかった試行はゲートに到達しない。
- **このループが得られない証拠は、そうと名指しする。** 受入基準が `external` の場合 —— ステージング
  確認・実機・人 —— 作業はマージされ、タスクは `awaiting-evidence` で待つ。誰かが見たものを
  `rein evidence record` で記録するまでである。その記録は対象としたツリーに束縛されるので、コードを
  変えれば失効する。
- **すべてはハッシュ連鎖したログに残る。** `.rein/events.ndjson` はすべての状態変化とその理由を記録し、
  ゲートの receipt は連鎖のルートを固定する。1行でも削除・再ハッシュされれば、その receipt が立って
  いる連鎖が壊れる。サンドボックスを digest で pin するのも同じ理由である: レビューが走った環境は、
  そのレビューが承認されたあとで変わってはならない。

reading が何を探すよう指示されたかは、習慣ではなくライブラリである。`rein lens --list` は各レビュー
レンズを、それが適用される条件とともに表示し、`--select <stage>` はその条件を plan に対して解決し、
`--stats` は適用回数と発見回数を示す —— 何も見つけないレンズは外せる。`rein observe` はハーネス自身に
ついて同じことを行い、各数値をそれが検証する主張の隣に印字する。閾値は無く、今後も追加しない: 上限の
ついた数値は、読まれるのではなく管理されるようになるからである。

## ビルドループ

`rein build` は、どのタスクを、どの並列度で、どのマージ順で実行し、いつ止まるかを決める —— LLM の
裁量ではなく `config.yaml`・`plan.yaml`・`state.yaml` から決定論的に導く(`--dry-run` はエージェント
CLI や git を呼ばずに制御フローだけを確認する)。手動で行う等価な手段は無い: `state.yaml` は機械が
書くものであり、リーフの判断はオーケストレータが提供するコントロールプレーンを通ってしか監査連鎖に
届かない。

タスクが done になるのは `config.yaml` の `quality_gate` —— **単一の DoD 定義** —— を通ったときだけで
ある。`test`、次に `check`、次に正しさと単純化のための `review` ステップ、次に実行可能な成果物に
対する `smoke` 起動(成果物が動くようになったら `required: true` にする)。各ステップは自分のリトライ
予算を持ち、使い切るとタスクは blocked になる。ステップは `paths:` で自分の適用範囲を絞れるので、
複数のスタックが同居するリポジトリが毎タスクで全スタック分のコストを払うことはない。コマンドは
プロジェクト自身のものである —— 同梱の既定値は同梱サンドボックスが動かせる下限であり、既存リポジトリ
では `rein init` が検出したコマンドを埋める。並列のリーフは `git worktree` で隔離して実行され
(`max_parallel` まで)、タスク ID の昇順にマージされる。

**無人での実行。** 終了コードが信号である: `0` は完了、`1` と `2` は人間を必要とし、`3` は一時的 ——
容量制限、シグナル、別の実行がロックを保持している —— で、何も記録せず予算も消費しないので再実行して
よい。`rein build --supervise` は `3` を自動でリトライし、`rein review generate --supervise` はレビュー
パイプライン側で同じことを行う。ただし「収まらなかったリクエスト」は対象外である —— それは何度試しても
同じ大きさだからである。

エージェントの起動には既定で**時間制限が無い**(`execution.agent_timeout_sec: 0`)。時計は、動いて
いるモデルと詰まっているモデルを区別できない。動いているものを殺せばその起動は捨てられ、リトライが
もう一度その分を払う。command ステップは所要時間が分かるので、上限を持ったままである。

既定では**費用の上限も無い**。設定するのは `execution.max_cost_usd` である。これが束縛するのは
サイクルの*計測された*消費 —— アダプタが報告した値、`rein events --cost` が印字する数字 —— であり、
到達するとバッチの切れ目でループが止まる。上限に収めるために品質が落ちることはない: 安いモデルにも
薄いレビューにも切り替えない。費用を報告しないアダプタの起動は、0 円として扱われるのではなく「数えら
れる」。したがって何ひとつ値付けできなかったサイクルもまた停止し、「無料だった」ではなく「上限が
効いていなかった」と述べる。

## セキュリティ

- **gitleaks** を pre-commit で実行する。誤検知は `.gitleaksignore` に入れる。
- **構造化されたセキュリティレビュー**を grounded review に畳み込み、レビュー対象の HEAD に束縛する。
  指摘は散文ではなく、深刻度・コード上のアンカー・blocking フラグを持ち、blocking のものはゲートを
  止める。
- **依存関係監査**を `/verify` で行う。acceptance はコードを読み直す代わりにレビューを携え、ツリーから
  決まらない唯一の答えをそこに加える: コードが止まっていてもデータベースは動くからである。

指摘には寿命がある。変更がそれを解消するまでは `open` であり、どちらが起きたかを次の生成が決めるのは、
レビュアーに尋ねることによってではなく、それがアンカーしたコードを読み直すことによってである。ツリー
から消えていれば、それを除去した head に対して `resolved` と記録される。まだ在れば、それを落とすことは
「レビュアーが自分のブロックを自分で解除する」行為として拒否される。アンカーの無い指摘は、人間による
異議申し立てによってのみ閉じる。

## 既存リポジトリへの導入(brownfield)

専用の導入コマンドは無く、`rein init` が既存のコードベースを自動検出する。そのモードでは
`guard.paths` を docs の成果物だけに絞り —— pending のゲートが既存コードを凍結しないようにするため
である —— 認識できる範囲で品質ゲートのコマンドを埋め、brief から `/onboard` を指す。既存ファイルが
上書きされることはない。そのうえでリポジトリの中で:

1. **`/onboard`** がコードベースを読み取り専用で調査し、恒久的なベースラインである
   `docs/05-current-state.md` を埋める。既存の振る舞いを要件や完了タスクへ逆生成することは**しない**。
   トレーサビリティが覆うのは各サイクルの差分だけである。途中まで進んでいた作業は *absorb タスク* が
   受け止め、その部分的なコードを green に固定してから新しい作業を積む。
2. **差分サイクル** —— brief から `/verify` までの1周が1つの変更を記述し、`rein cycle-close` で閉じる。
   brief と `docs/05-current-state.md` はサイクルをまたいで残る。
3. **いつでも撤回できる** —— `rein uninstall claude|copilot|codex|gemini` はエージェント連携面を撤去し
   (未変更のファイルのみ。設定のマージはエントリ単位で戻す)、`rein uninstall --all` は実体化された
   成果物とロックをすべて削除する。SSOT と `docs/` には触れない。

## トラブルシューティング

**まず `rein doctor`** —— PATH 上の実行ファイルやフック登録から、plan と state の整合、レビューの
鮮度、サンドボックスの pin、スキーマ検証まで、全体を読み取り専用で診断する。以下の多くはここに現れる。

- **タスクが `blocked` になった** —— 品質ゲートがリトライ予算の内で失敗した。`rein events --render` で
  エスカレーションを読み、原因を直し、`rein task reset T-NNN --reason "…"` でタスクをフロンティアに
  戻す。`state.yaml` の手編集では行わない: それはストアのトランザクション内でのみ書かれ、`rein guard` が
  手編集を拒否する。reset は handoff を保持するので、リトライ予算が黙って補充されることはない
  (`--fresh` は破棄し、そう述べる)。原因が上流の不備なら `/revise <phase>` を使う。
- **実行が止まったが、どこもおかしく見えない** —— blocked のタスクもエスカレーションも無く、ボードも
  動いていない。これはタスクの失敗ではなく機械の失敗である: 容量制限、プロセスの kill、CLI の不在。
  `rein doctor` と `rein start` がそれを名指しする。終了コードが `3` なら、容量が戻ってから
  `rein build` を再実行すればよい。各タスクは状態もリトライ予算も保持している。
- **ループが中断した**(Ctrl-C、クラッシュ)—— `rein build` を再実行する。起動時に `in-progress` の
  タスクを戻し、残った worktree を掃除する。中断されたリーフのコミットは salvage ブランチに保持され、
  次の試行にマージして戻される(コンフリクトは報告され、強制はされない)。
- **ゲートガードに編集を拒否された** —— ゲートが pending の状態で次フェーズの成果物を編集している。
  これは仕組みが働いている状態である。ゲートを承認してもらうか、`/revise` で差し戻す。迂回路は無い。
- **「template placeholders」と言われる** —— 先に `rein start`(または `rein init --name <product>`)を
  実行する。
- **フックで `rein: command not found`** —— CLI が PATH 上に無い(セットアップの手順2)。
- **フェーズコマンドがエージェントに現れない** —— 連携面が未導入である:
  `rein install claude|copilot|codex|gemini` を実行し、新しいセッションを開く(セットアップの手順5)。

## リポジトリ構成

`rein init` が書き込むのは**状態だけ**である: SSOT の各文書、docs のスキャフォールド、実体化された
プロンプトとスキーマおよびその pristine スナップショット、ロック、`AGENTS.md` に追記されるマーカー
付きのポインタブロック、そして作業ブランチ。ビルドファイルは書かず、`rein install` しない限り
エージェント連携面も書かない。既存ファイルが上書きされることはない。オーケストレーションのコード自体は
インストール済みパッケージの中にあり、リポジトリには入らない。

| パス | 役割 |
|---|---|
| `.rein/plan.yaml` | 凍結された Expected Model: 要件ごとの claim と、タスク DAG |
| `.rein/state.yaml` | 可変の状態: フェーズ、ゲート承認、タスクの状態 |
| `.rein/review.yaml` | 機械レビューと人間レビュー(別々に digest される) |
| `.rein/events.ndjson` | ハッシュ連鎖した監査ログ。各起動はプロバイダが課金した量を記録するので、`rein events --cost` がサイクルのトークンの行き先を役割別に答える |
| `.rein/config.yaml` | 決定論的実行のつまみと、単一の DoD (`quality_gate`) |
| `.rein/rein.lock` | 文書フォーマット、ツールのバージョンと取得元、導入ファイルごとの内容ハッシュ |
| `.rein/schema/`・`.rein/prompts/` | SSOT の JSON Schema と、全エージェントが読むフェーズ手順・役割定義・ルールモジュール(いずれも実体化) |
| `AGENTS.md`・`CLAUDE.md` | エージェント非依存の運用ルールと、それを読み込む Claude Code 用の能力マッピング |
| `.claude/`・`.github/` | エージェントごとの入口とゲートガードのフック登録(`rein install` によるオプトイン) |
| `docs/` | 各フェーズの成果物、投機的作業ログ、レトロスペクティブ |

## エージェント対応

Loose Rein は **Claude Code** と **VS Code GitHub Copilot**(フック強制のゲートを含む完全対応)、
そして **Codex**・**Gemini CLI**・`AGENTS.md` を読むその他のエージェント(ルールと手順は伝わり、ゲートは
規約として機能する)で動作する。ルールは人間とのやり取りが必要な箇所をすべて**能力語彙**で名指しし、
それを何で実現するかは各エージェントのマッピングファイルが述べる。自前のマッピングを持たない
エージェントは、`AGENTS.md` の能力語彙表にある degradation 列に従う。

| 能力 | Claude Code | VS Code Copilot | Codex | Gemini CLI |
|---|---|---|---|---|
| フェーズの入口 | スラッシュコマンド | prompt ファイル | skills | カスタムコマンド |
| ゲートの強制 | PreToolUse フック | agent hooks(プレビュー) | `apply_patch` フック | `BeforeTool` フック |
| 構造化された質問 | AskUserQuestion | チャットの番号付き選択肢 | チャットの番号付き選択肢 | チャットの番号付き選択肢 |
| 承認の提示 | plan mode | plan mode | 明示的な「承認」 | 明示的な「承認」 |
| 役割の委譲 | subagents | カスタムエージェント | subagents | skills |
| ビルドの CLI として選択 | `rein agent claude` | `rein agent copilot` | `rein agent codex` | `rein agent gemini` |
| ゲート待ちの通知 | PushNotification | ターン終了 | ターン終了 | ターン終了 |

どのホストでも commit 段階の検査は働き、フックが無い場所を支えるのはそれである。注意点が3つある。
Codex 向けの連携面は**実機の Codex に対して未検証**である(フックのペイロード形状、探索パス、
アダプタのフラグは、観測されたセッションではなく openai/codex のソースとドキュメントに基づく。加えて
Codex はプロジェクトが信頼されるまでプロジェクト単位の設定を読まない)。VS Code Copilot の agent hooks は
**プレビュー**機能であり、無効なら規約としてゲートが機能する。そして委譲が使えない環境では、並列の
リーフタスクは直列に縮退する。どのフックホストが登録されているかは `rein doctor` が報告する。
