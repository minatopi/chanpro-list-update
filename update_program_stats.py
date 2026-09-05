import os
import re
import json
import subprocess
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from playwright.sync_api import sync_playwright


# ============================================================
# 設定
# ============================================================

DATABASE_URL = os.environ["DATABASE_URL"]

# GitHub Actions上でJSONを保存する場所
STATE_FILE = "program_likes.json"

# ChanProプロフィールURL
PROFILE_URL_PREFIX = "https://chanpro.jp/00-program-profile/"

# Playwright待機時間
PROFILE_WAIT_MS = 8000

# GitHub Actionsで自動commit/pushするか
# GitHub Actionsなら 1 にしておく
AUTO_GIT_PUSH = os.environ.get("AUTO_GIT_PUSH", "1") == "1"


# ============================================================
# 共通
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def safe_int(value, default=0):
    try:
        if value is None:
            return default

        if isinstance(value, int):
            return value

        text = str(value).replace(",", "").strip()

        m = re.search(r"\d+", text)

        if not m:
            return default

        return int(m.group(0))

    except Exception:
        return default


# ============================================================
# JSON読み込み
# ============================================================

def load_state():
    """
    前回のいいね数を保存したJSONを読み込む。
    初回は空データ。
    """

    if not os.path.exists(STATE_FILE):
        print("--------------------------------------------------")
        print(f"{STATE_FILE} がありません。初回実行として処理します。")
        print("初回は通知を作成しません。")
        print("--------------------------------------------------")
        return {
            "updated_at": None,
            "users": {}
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("JSONの形式が不正です")

        if "users" not in data:
            data["users"] = {}

        return data

    except Exception as e:
        print("JSON読み込みエラー:")
        print(e)

        # 壊れたJSONの場合は初回扱い
        return {
            "updated_at": None,
            "users": {}
        }


# ============================================================
# JSON保存
# ============================================================

def save_state(state):
    state["updated_at"] = now_iso()

    tmp_file = STATE_FILE + ".tmp"

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(tmp_file, STATE_FILE)

    print()
    print("==================================================")
    print(f"{STATE_FILE} を保存しました")
    print("==================================================")


# ============================================================
# PostgreSQL
# ============================================================

def get_user_columns(conn):
    """
    usersテーブルのカラム一覧を取得。
    """

    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'users'
            ORDER BY ordinal_position
        """)

        return [row[0] for row in cur.fetchall()]


# ============================================================
# プロフィールURL検出
# ============================================================

def extract_profile_urls(value):
    """
    DBの値の中からChanProプロフィールURLを探す。

    text
    list
    tuple
    dict

    などに対応。
    """

    urls = []

    if value is None:
        return urls

    # 文字列
    if isinstance(value, str):
        matches = re.findall(
            r'https?://chanpro\.jp/00-program-profile/[^\s"\'<>]+',
            value
        )

        for url in matches:
            url = url.rstrip("),]}>'\"")
            if url not in urls:
                urls.append(url)

        return urls

    # 配列
    if isinstance(value, (list, tuple)):
        for item in value:
            for url in extract_profile_urls(item):
                if url not in urls:
                    urls.append(url)

        return urls

    # 辞書
    if isinstance(value, dict):
        for item in value.values():
            for url in extract_profile_urls(item):
                if url not in urls:
                    urls.append(url)

        return urls

    return urls


def find_profile_url_from_user(row):
    """
    usersテーブルの1ユーザー分のデータから
    ChanProプロフィールURLを探す。

    優先順位:
    1. profile_url
    2. profile_urls
    3. chanpro_profile_url
    4. chanpro_profile_urls
    5. その他すべてのカラム
    """

    priority_columns = [
        "profile_url",
        "profile_urls",
        "chanpro_profile_url",
        "chanpro_profile_urls",
        "program_profile_url",
        "program_profile_urls",
    ]

    # --------------------------------------------------------
    # 優先カラム
    # --------------------------------------------------------

    for column in priority_columns:

        if column not in row:
            continue

        urls = extract_profile_urls(row[column])

        if urls:
            return urls[0]

    # --------------------------------------------------------
    # 全カラム検索
    # --------------------------------------------------------

    for column, value in row.items():

        # 明らかに不要なものは除外
        if column in [
            "password_hash",
        ]:
            continue

        urls = extract_profile_urls(value)

        if urls:
            return urls[0]

    return None


# ============================================================
# ユーザー取得
# ============================================================

def get_users(conn):
    """
    usersテーブルからユーザーを取得。
    """

    columns = get_user_columns(conn)

    print()
    print("usersテーブルのカラム:")
    print(", ".join(columns))

    with conn.cursor(row_factory=dict_row) as cur:

        cur.execute("""
            SELECT *
            FROM public.users
            ORDER BY created_at NULLS LAST
        """)

        rows = cur.fetchall()

    users = []

    for row in rows:

        user_id = row.get("id")

        if not user_id:
            continue

        profile_url = find_profile_url_from_user(row)

        # program_urls
        program_urls = row.get("program_urls") or []

        if isinstance(program_urls, str):
            program_urls = [program_urls]

        users.append({
            "id": str(user_id),
            "profile_url": profile_url,
            "program_urls": program_urls,
            "row": row
        })

    return users


# ============================================================
# プロフィールカード解析
# ============================================================

def parse_card(text):
    """
    プロフィールカードのテキストを解析。

    既存の動作していた方式をそのままベースにする。

    例:

    じゃんけんゲーム
    Lv.10
    1252
    3045

    ↓

    title = じゃんけんゲーム
    like  = 1252
    views = 3045
    """

    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    # 不要な文字を除外
    lines = [
        line
        for line in lines
        if line not in ["ログイン"]
        and not line.startswith("Lv.")
    ]

    if not lines:
        return None

    title = lines[0]

    # 数字取得
    nums = re.findall(r"\d[\d,]*", text)

    numbers = []

    for n in nums:
        try:
            numbers.append(
                int(n.replace(",", ""))
            )
        except Exception:
            pass

    like_count = numbers[0] if len(numbers) >= 1 else 0
    views_count = numbers[1] if len(numbers) >= 2 else 0

    return {
        "title": title,
        "likes": like_count,
        "views": views_count
    }


# ============================================================
# カードからURL取得
# ============================================================

def get_card_url(card):
    """
    カード内にリンクが存在する場合は取得。

    URLが取れなくても処理は続行。
    """

    try:

        links = card.locator("a")

        count = links.count()

        for i in range(count):

            try:
                href = links.nth(i).get_attribute("href")

                if not href:
                    continue

                if href.startswith("/"):
                    href = "https://chanpro.jp" + href

                if href.startswith("http"):
                    return href

            except Exception:
                continue

    except Exception:
        pass

    return None


# ============================================================
# プロフィールページ取得
# ============================================================

def scrape_profile(page, profile_url):
    """
    プロフィールページから作品一覧を取得。

    カードをクリックしない。
    """

    print()
    print("--------------------------------------------------")
    print("プロフィール取得")
    print(profile_url)
    print("--------------------------------------------------")

    result = []

    try:

        page.goto(
            profile_url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(PROFILE_WAIT_MS)

        # ----------------------------------------------------
        # 既存の動作確認済みセレクタ
        # ----------------------------------------------------

        container = page.locator(
            "div.bubble-element.Group.baTcwaH1"
        ).first

        container.wait_for(
            state="visible",
            timeout=30000
        )

        cards = container.locator(
            "div.clickable-element"
        )

        count = cards.count()

        print(f"作品カード数: {count}")

        for i in range(count):

            try:

                card = cards.nth(i)

                text = card.inner_text()

                print()
                print(f"--- CARD {i + 1} ---")
                print(text)

                parsed = parse_card(text)

                if not parsed:
                    continue

                card_url = get_card_url(card)

                parsed["url"] = card_url

                # 作品を識別するキー
                #
                # URLがあればURLを優先。
                # URLが取れなければタイトル。
                if card_url:
                    key = card_url
                else:
                    key = parsed["title"]

                parsed["key"] = key

                result.append(parsed)

                print(
                    f"タイトル: {parsed['title']}"
                )

                print(
                    f"いいね: {parsed['likes']}"
                )

                print(
                    f"閲覧数: {parsed['views']}"
                )

                if card_url:
                    print(
                        f"作品URL: {card_url}"
                    )

            except Exception as e:

                print(
                    f"CARD {i + 1} 取得エラー:"
                )
                print(e)

    except Exception as e:

        print()
        print("プロフィール取得失敗")
        print(profile_url)
        print(e)

    return result


# ============================================================
# program_likes.json のユーザー情報
# ============================================================

def get_old_user_state(state, user_id):
    users = state.get("users", {})

    user_state = users.get(str(user_id))

    if not isinstance(user_state, dict):
        return {
            "profile_url": None,
            "programs": {}
        }

    programs = user_state.get("programs", {})

    if not isinstance(programs, dict):
        programs = {}

    return {
        "profile_url": user_state.get("profile_url"),
        "programs": programs
    }


# ============================================================
# いいね比較
# ============================================================

def compare_likes(
    old_programs,
    current_programs
):
    """
    前回と今回を比較。

    初回:
        通知しない

    前回 1251
    今回 1252
        ↓
    +1なので通知

    前回 1252
    今回 1252
        ↓
    通知なし

    前回 1252
    今回 1250
        ↓
    通知なし
    """

    increases = []

    for program in current_programs:

        key = program["key"]

        current_likes = safe_int(
            program.get("likes"),
            0
        )

        old_data = old_programs.get(key)

        # -----------------------------------------------
        # 初回
        # -----------------------------------------------

        if old_data is None:
            print()
            print(
                f"初回登録: {program['title']} "
                f"いいね={current_likes}"
            )

            continue

        old_likes = safe_int(
            old_data.get("likes"),
            0
        )

        diff = current_likes - old_likes

        print()
        print(
            f"比較: {program['title']}"
        )

        print(
            f"  前回: {old_likes}"
        )

        print(
            f"  今回: {current_likes}"
        )

        print(
            f"  増減: {diff:+d}"
        )

        if current_likes > old_likes:

            increases.append({
                "key": key,
                "title": program["title"],
                "old_likes": old_likes,
                "new_likes": current_likes,
                "increase": diff
            })

    return increases


# ============================================================
# 通知作成
# ============================================================

def create_like_notification(
    conn,
    user_id,
    title
):
    """
    notificationsに「いいね」通知を保存。
    """

    message = f"「{title}」にいいねがされました"

    print()
    print("★ 通知作成")
    print(f"  ユーザーID: {user_id}")
    print(f"  作品: {title}")
    print(f"  メッセージ: {message}")

    try:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO public.notifications (
                    user_id,
                    actor_id,
                    type,
                    post_id,
                    created_at,
                    is_read,
                    message
                )
                VALUES (
                    %s,
                    NULL,
                    'like',
                    NULL,
                    NOW(),
                    FALSE,
                    %s
                )
                RETURNING id
            """, (
                user_id,
                message
            ))

            row = cur.fetchone()

            notification_id = row[0] if row else None

        conn.commit()

        print(
            "  ★ 通知保存成功"
        )

        print(
            f"  notification_id: {notification_id}"
        )

        return True

    except Exception as e:

        print()
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("通知保存失敗")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")

        print(
            f"user_id = {user_id}"
        )

        print(
            f"title = {title}"
        )

        print(
            f"message = {message}"
        )

        print(
            "SQL ERROR:"
        )

        print(
            repr(e)
        )

        print()

        try:
            conn.rollback()
        except Exception:
            pass

        return False


# ============================================================
# users.program_views / program_likes 更新
# ============================================================

def get_all_programs(conn):
    """
    users.program_urlsに登録されている作品URLを
    全ユーザー分取得して重複排除。
    """

    programs = set()

    with conn.cursor() as cur:

        cur.execute("""
            SELECT program_urls
            FROM public.users
            WHERE program_urls IS NOT NULL
        """)

        rows = cur.fetchall()

    for row in rows:

        urls = row[0] or []

        if isinstance(urls, str):
            urls = [urls]

        for url in urls:

            if not url:
                continue

            url = str(url).strip()

            if url:
                programs.add(url)

    return sorted(programs)


# ============================================================
# 個別作品ページの統計取得
# ============================================================

def get_number_from_icon(page, icon_name):
    """
    #eyes / #heart のアイコンを探して、
    同じGroupの中にある数字を取得。
    """

    try:

        icon = page.locator(
            f'use[href*="#{icon_name}"]'
        )

        if icon.count() == 0:
            return None

        group = icon.first.locator(
            "xpath=ancestor::div[contains(@class, 'Group')][1]"
        )

        if group.count() == 0:
            return None

        text = group.inner_text()

        numbers = re.findall(
            r"\d[\d,]*",
            text
        )

        if not numbers:
            return None

        return int(
            numbers[-1].replace(",", "")
        )

    except Exception as e:

        print(
            f"{icon_name}取得エラー: {e}"
        )

        return None


def get_program_data(page, url):
    """
    個別プログラムページから
    閲覧数・いいね数を取得。
    """

    print()
    print("==================================================")
    print("作品統計取得")
    print(url)
    print("==================================================")

    try:

        page.goto(
            url,
            wait_until="networkidle",
            timeout=60000
        )

        page.wait_for_timeout(3000)

        views = get_number_from_icon(
            page,
            "eyes"
        )

        likes = get_number_from_icon(
            page,
            "heart"
        )

        print(
            f"閲覧数: {views}"
        )

        print(
            f"いいね: {likes}"
        )

        return {
            "url": url,
            "views": views,
            "likes": likes,
            "status": "success"
        }

    except Exception as e:

        print()
        print("作品取得失敗")
        print(url)
        print(str(e))

        return {
            "url": url,
            "views": None,
            "likes": None,
            "status": "error",
            "error": str(e)
        }


# ============================================================
# usersテーブル更新
# ============================================================

def update_users(conn, stats):
    """
    users.program_views
    users.program_likes

    を更新。
    """

    print()
    print("==================================================")
    print("users.program_views / program_likes 更新")
    print("==================================================")

    with conn.cursor() as cur:

        cur.execute("""
            SELECT id, program_urls
            FROM public.users
            WHERE program_urls IS NOT NULL
        """)

        rows = cur.fetchall()

        updated_users = 0

        for user_id, program_urls in rows:

            program_urls = program_urls or []

            if isinstance(program_urls, str):
                program_urls = [program_urls]

            views = []
            likes = []

            for url in program_urls:

                data = stats.get(
                    str(url).strip()
                )

                if data is None:

                    views.append(0)
                    likes.append(0)

                else:

                    views.append(
                        data["views"]
                        if data["views"] is not None
                        else 0
                    )

                    likes.append(
                        data["likes"]
                        if data["likes"] is not None
                        else 0
                    )

            cur.execute("""
                UPDATE public.users
                SET
                    program_views = %s,
                    program_likes = %s
                WHERE id = %s
            """, (
                views,
                likes,
                user_id
            ))

            updated_users += 1

    conn.commit()

    print(
        f"users更新完了: {updated_users}人"
    )

    return updated_users


# ============================================================
# GitHubへJSONをcommit/push
# ============================================================

def git_push_state():
    """
    GitHub Actions上で
    program_likes.jsonをcommit/pushする。

    AUTO_GIT_PUSH=0 の場合は無効。
    GitHub Actions以外ではpushしない。
    """

    if not AUTO_GIT_PUSH:
        print()
        print("GitHub自動pushは無効です。")
        return

    if os.environ.get("GITHUB_ACTIONS") != "true":
        print()
        print(
            "GitHub Actionsではないため、"
            "自動pushをスキップします。"
        )
        return

    print()
    print("==================================================")
    print("GitHub JSON更新")
    print("==================================================")

    try:
        # --------------------------------------------------------
        # Gitユーザー設定
        # --------------------------------------------------------

        subprocess.run(
            [
                "git",
                "config",
                "user.name",
                "github-actions[bot]"
            ],
            check=True
        )

        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "41898282+github-actions[bot]@users.noreply.github.com"
            ],
            check=True
        )

        # --------------------------------------------------------
        # 現在のブランチ確認
        # --------------------------------------------------------

        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True
        )

        branch = branch_result.stdout.strip()

        # actions/checkoutではdetached HEADになることがあるため、
        # GITHUB_REF_NAMEを使用
        if not branch:
            branch = os.environ.get(
                "GITHUB_REF_NAME",
                ""
            ).strip()

        if not branch:
            branch = "main"

        print(f"Push先ブランチ: {branch}")

        # --------------------------------------------------------
        # JSONファイルを強制的にstage
        #
        # .gitignoreに入っていても追加できるように -f を使用
        # --------------------------------------------------------

        subprocess.run(
            [
                "git",
                "add",
                "-f",
                STATE_FILE
            ],
            check=True
        )

        # --------------------------------------------------------
        # Git状態確認
        # --------------------------------------------------------

        status_result = subprocess.run(
            [
                "git",
                "status",
                "--short"
            ],
            capture_output=True,
            text=True,
            check=True
        )

        print()
        print("Git status:")
        print(status_result.stdout)

        # --------------------------------------------------------
        # stageされた変更があるか確認
        # --------------------------------------------------------

        result = subprocess.run(
            [
                "git",
                "diff",
                "--cached",
                "--quiet"
            ]
        )

        if result.returncode == 0:
            print(
                "program_likes.jsonに変更はありません。"
            )
            return

        # --------------------------------------------------------
        # commit
        # --------------------------------------------------------

        commit_message = (
            "Update program like state "
            + datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        )

        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                commit_message
            ],
            check=True
        )

        # --------------------------------------------------------
        # push
        # --------------------------------------------------------

        push_result = subprocess.run(
            [
                "git",
                "push",
                "origin",
                f"HEAD:{branch}"
            ],
            capture_output=True,
            text=True
        )

        print()
        print("git push stdout:")
        print(push_result.stdout)

        print("git push stderr:")
        print(push_result.stderr)

        if push_result.returncode != 0:
            raise RuntimeError(
                "git push failed: "
                f"returncode={push_result.returncode}"
            )

        print()
        print("GitHubへのpush完了")

    except Exception as e:
        print()
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("GitHub push失敗")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print()
        print("原因:")
        print(repr(e))
        print()

        # GitHub Actionsでは失敗として終了させる。
        # これにより、push失敗を「処理完了」と誤認しない。
        raise


# ============================================================
# メイン
# ============================================================

def main():

    started_at = now_iso()

    print()
    print("=" * 70)
    print("ChanPro いいね監視システム")
    print("=" * 70)
    print(
        f"開始: {started_at}"
    )
    print()

    # --------------------------------------------------------
    # 前回JSON
    # --------------------------------------------------------

    state = load_state()

    old_users = state.get(
        "users",
        {}
    )

    first_run = len(old_users) == 0

    print()
    print(
        f"前回保存ユーザー数: {len(old_users)}"
    )

    if first_run:
        print(
            "★ 初回実行です。通知は発生させません。"
        )

    # --------------------------------------------------------
    # PostgreSQL
    # --------------------------------------------------------

    with psycopg.connect(
        DATABASE_URL
    ) as conn:

        # ----------------------------------------------------
        # ユーザー取得
        # ----------------------------------------------------

        users = get_users(conn)

        print()
        print("=" * 70)
        print(
            f"ユーザー数: {len(users)}"
        )
        print("=" * 70)

        # ----------------------------------------------------
        # Playwright
        # ----------------------------------------------------

        profile_results = {}

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True
            )

            page = browser.new_page(
                viewport={
                    "width": 1280,
                    "height": 2000
                },
                user_agent=(
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/120.0.0.0 "
                    "Safari/537.36"
                )
            )

            # =================================================
            # ① ユーザーごとのプロフィールを取得
            # =================================================

            for index, user in enumerate(users, start=1):

                user_id = user["id"]

                profile_url = user["profile_url"]

                print()
                print()
                print("#" * 70)
                print(
                    f"USER {index}/{len(users)}"
                )
                print(
                    f"ユーザーID: {user_id}"
                )
                print(
                    f"プロフィールURL: {profile_url}"
                )
                print("#" * 70)

                if not profile_url:

                    print(
                        "⚠ プロフィールURLが見つかりません。"
                    )

                    profile_results[user_id] = []

                    continue

                works = scrape_profile(
                    page,
                    profile_url
                )

                profile_results[user_id] = works

                # ------------------------------------------------
                # ② いいね比較
                # ------------------------------------------------

                old_user = get_old_user_state(
                    state,
                    user_id
                )

                old_programs = old_user.get(
                    "programs",
                    {}
                )

                increases = compare_likes(
                    old_programs,
                    works
                )

                # ------------------------------------------------
                # ③ 通知作成
                # ------------------------------------------------

                if increases:

                    print()
                    print("★ いいね増加検出")

                for increase in increases:

                    print()
                    print(
                        f"  ユーザーID: {user_id}"
                    )

                    print(
                        f"  作品: {increase['title']}"
                    )

                    print(
                        f"  前回: {increase['old_likes']}"
                    )

                    print(
                        f"  今回: {increase['new_likes']}"
                    )

                    print(
                        f"  増加: +{increase['increase']}"
                    )

                    # 初回は通知しない
                    if first_run:
                        continue

                    create_like_notification(
                        conn,
                        user_id,
                        increase["title"]
                    )

            browser.close()

        # =====================================================
        # ④ program_likes.jsonを最新状態にする
        # =====================================================

        print()
        print("=" * 70)
        print("いいね状態JSON更新")
        print("=" * 70)

        new_users_state = {}

        for user in users:

            user_id = user["id"]

            profile_url = user["profile_url"]

            works = profile_results.get(
                user_id,
                []
            )

            programs = {}

            for work in works:

                key = work["key"]

                programs[key] = {
                    "title": work["title"],
                    "url": work.get("url"),
                    "likes": safe_int(
                        work.get("likes"),
                        0
                    ),
                    "views": safe_int(
                        work.get("views"),
                        0
                    ),
                    "updated_at": now_iso()
                }

            new_users_state[user_id] = {
                "profile_url": profile_url,
                "programs": programs,
                "updated_at": now_iso()
            }

        state = {
            "updated_at": now_iso(),
            "users": new_users_state
        }

        save_state(state)

        # =====================================================
        # ⑤ 既存の作品URL統計取得
        # =====================================================

        programs = get_all_programs(
            conn
        )

        print()
        print("=" * 70)
        print(
            f"個別作品統計取得対象: {len(programs)}"
        )
        print("=" * 70)

        stats = {}

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True
            )

            page = browser.new_page(
                viewport={
                    "width": 1280,
                    "height": 2000
                },
                user_agent=(
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/120.0.0.0 "
                    "Safari/537.36"
                )
            )

            for url in programs:

                result = get_program_data(
                    page,
                    url
                )

                stats[url] = result

            browser.close()

        # =====================================================
        # ⑥ usersテーブル更新
        # =====================================================

        updated_users = update_users(
            conn,
            stats
        )

    # =========================================================
    # ⑦ result.json
    # =========================================================

    output = {
        "fetched_at": started_at,
        "finished_at": now_iso(),
        "user_count": len(users),
        "program_count": len(programs),
        "updated_users": updated_users,
        "programs": stats
    }

    with open(
        "result.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            output,
            f,
            ensure_ascii=False,
            indent=2
        )

    print()
    print("=" * 70)
    print("result.json 保存完了")
    print("=" * 70)

    # =========================================================
    # ⑧ GitHubへpush
    # =========================================================

    git_push_state()

    # =========================================================
    # 完了
    # =========================================================

    print()
    print()
    print("=" * 70)
    print("すべての処理が完了しました")
    print("=" * 70)
    print(
        f"ユーザー数       : {len(users)}"
    )
    print(
        f"作品統計取得数   : {len(programs)}"
    )
    print(
        f"DBユーザー更新数 : {updated_users}"
    )
    print(
        f"完了時刻         : {now_iso()}"
    )
    print("=" * 70)


# ============================================================
# 実行
# ============================================================

if __name__ == "__main__":
    main()
