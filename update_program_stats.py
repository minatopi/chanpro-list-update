import os
import re
import json
import subprocess
from datetime import datetime, timezone

import psycopg
from playwright.sync_api import sync_playwright


# ============================================================
# 設定
# ============================================================

DATABASE_URL = os.environ["DATABASE_URL"]

# このPythonファイルと同じフォルダ
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# GitHubで管理するJSON
JSON_FILE = os.path.join(BASE_DIR, "program_stats.json")

# Playwright
WAIT_AFTER_LOAD = 8000

# 同じ内容の通知を短時間に何度も作らないための時間
DUPLICATE_MINUTES = 10


# ============================================================
# 共通
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run_git(*args):
    """
    gitコマンドを実行
    """
    result = subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        print("Gitエラー:")
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            f"git {' '.join(args)} が失敗しました"
        )

    return result.stdout.strip()


# ============================================================
# GitHub JSON読み込み
# ============================================================

def load_previous_stats():
    """
    前回の program_stats.json を読み込む
    """

    if not os.path.exists(JSON_FILE):
        print("program_stats.json がありません。新規作成します。")

        return {
            "updated_at": None,
            "profiles": {}
        }

    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("JSONの形式が不正です")

        data.setdefault("updated_at", None)
        data.setdefault("profiles", {})

        return data

    except Exception as e:
        print("program_stats.json の読み込みに失敗:")
        print(e)

        return {
            "updated_at": None,
            "profiles": {}
        }


# ============================================================
# GitHub JSON保存
# ============================================================

def save_stats(data):
    """
    program_stats.jsonを保存
    """

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    print(f"JSON保存完了: {JSON_FILE}")


# ============================================================
# 数字抽出
# ============================================================

def extract_number(text):
    """
    文字列から最初の整数を取得
    """

    if not text:
        return None

    match = re.search(r"([\d,]+)", text)

    if not match:
        return None

    return int(match.group(1).replace(",", ""))


# ============================================================
# プロジェクトカード解析
# ============================================================

def parse_project_card(card):
    """
    1つのプロジェクトカードから

    title
    likes
    views
    total

    を取得
    """

    texts = []

    try:
        elements = card.locator("div.bubble-element.Text")

        count = elements.count()

        for i in range(count):
            try:
                text = elements.nth(i).inner_text().strip()

                if text:
                    texts.append(text)

            except Exception:
                pass

    except Exception:
        pass

    if not texts:
        return None

    # --------------------------------------------------------
    # タイトル
    # --------------------------------------------------------

    title = texts[0].strip()

    if not title:
        return None

    likes = None
    views = None
    total = None

    # --------------------------------------------------------
    # カード内テキストから数字を探す
    # --------------------------------------------------------

    for text in texts:

        # 例:
        # 18こ
        # 18 こ
        # 18こいいね
        m = re.search(r"([\d,]+)\s*こ", text)

        if m:
            try:
                likes = int(m.group(1).replace(",", ""))
            except Exception:
                pass

        # 例:
        # 123 / 1000
        m = re.search(r"([\d,]+)\s*/\s*([\d,]+)", text)

        if m:
            try:
                views = int(m.group(1).replace(",", ""))
                total = int(m.group(2).replace(",", ""))
            except Exception:
                pass

    # --------------------------------------------------------
    # いいねが見つからなかった場合
    # --------------------------------------------------------

    if likes is None:

        for text in texts[1:]:

            # 「いいね」などの文字が含まれる場合
            if "いいね" in text:

                n = extract_number(text)

                if n is not None:
                    likes = n
                    break

    # --------------------------------------------------------
    # 視聴数が見つからない場合
    # --------------------------------------------------------

    if views is None:

        for text in texts[1:]:

            if "再生" in text or "閲覧" in text or "アクセス" in text:

                n = extract_number(text)

                if n is not None:
                    views = n
                    break

    # --------------------------------------------------------
    # 見つからなかった値は0
    # --------------------------------------------------------

    if likes is None:
        likes = 0

    if views is None:
        views = 0

    if total is None:
        total = 0

    return {
        "title": title,
        "likes": likes,
        "views": views,
        "total": total
    }


# ============================================================
# プロフィールからプロジェクト取得
# ============================================================

def scrape_profile(page, url):
    """
    プロフィールページから全プロジェクトを取得
    """

    print()
    print("=" * 70)
    print("プロフィール:")
    print(url)
    print("=" * 70)

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(WAIT_AFTER_LOAD)

    except Exception as e:

        print("ページを開けませんでした:")
        print(e)

        return None

    # --------------------------------------------------------
    # プロジェクトカード
    # --------------------------------------------------------

    cards = page.locator("div.clickable-element")

    try:
        count = cards.count()

    except Exception:
        count = 0

    print(f"カード数: {count}")

    projects = []

    for i in range(count):

        try:

            card = cards.nth(i)

            project = parse_project_card(card)

            if not project:
                continue

            print(
                f"[{i + 1}] "
                f"{project['title']} "
                f"いいね={project['likes']} "
                f"閲覧={project['views']}"
            )

            projects.append(project)

        except Exception as e:

            print(f"カード {i + 1} の解析失敗: {e}")

    # --------------------------------------------------------
    # 重複タイトル除去
    # --------------------------------------------------------

    unique_projects = []

    seen = set()

    for project in projects:

        title = project["title"]

        if title in seen:
            continue

        seen.add(title)

        unique_projects.append(project)

    projects = unique_projects

    # --------------------------------------------------------
    # 合計
    # --------------------------------------------------------

    total_likes = sum(
        project["likes"]
        for project in projects
    )

    total_views = sum(
        project["views"]
        for project in projects
    )

    print()
    print(f"プロジェクト数: {len(projects)}")
    print(f"いいね合計: {total_likes}")
    print(f"閲覧合計: {total_views}")

    return {
        "projects": projects,
        "total_likes": total_likes,
        "total_views": total_views
    }


# ============================================================
# 直近の重複通知チェック
# ============================================================

def notification_exists(conn, user_id, message):
    """
    同じ通知が直近10分以内に存在するか確認
    """

    sql = """
        SELECT id
        FROM notifications
        WHERE user_id = %s
          AND message = %s
          AND created_at >= NOW() - INTERVAL '10 minutes'
        LIMIT 1
    """

    with conn.cursor() as cur:

        cur.execute(
            sql,
            (
                user_id,
                message
            )
        )

        row = cur.fetchone()

    return row is not None


# ============================================================
# 通知作成
# ============================================================

def create_like_notification(
    conn,
    user_id,
    title
):
    """
    いいね増加通知を作成
    """

    message = f"「{title}」がいいねされました"

    # --------------------------------------------------------
    # 重複防止
    # --------------------------------------------------------

    if notification_exists(
        conn,
        user_id,
        message
    ):

        print(
            f"通知スキップ（重複）: {message}"
        )

        return False

    # --------------------------------------------------------
    # 通知INSERT
    # --------------------------------------------------------

    sql = """
        INSERT INTO notifications
        (
            user_id,
            actor_id,
            type,
            post_id,
            created_at,
            message_id,
            is_read,
            message
        )
        VALUES
        (
            %s,
            NULL,
            'like',
            NULL,
            NOW(),
            NULL,
            FALSE,
            %s
        )
    """

    with conn.cursor() as cur:

        cur.execute(
            sql,
            (
                user_id,
                message
            )
        )

    print(
        f"通知作成: user={user_id} "
        f"message={message}"
    )

    return True


# ============================================================
# 前回値との比較
# ============================================================

def compare_and_notify(
    conn,
    previous_profile,
    current_profile,
    user_id
):
    """
    プロジェクトごとのいいね数を比較
    """

    previous_projects = {}

    if previous_profile:

        for project in previous_profile.get(
            "projects",
            []
        ):

            title = project.get("title")

            if title:
                previous_projects[title] = project

    current_projects = current_profile.get(
        "projects",
        []
    )

    notification_count = 0

    for current in current_projects:

        title = current["title"]

        current_likes = int(
            current.get("likes", 0)
        )

        previous = previous_projects.get(title)

        # ----------------------------------------------------
        # 初回登場プロジェクト
        # ----------------------------------------------------

        if previous is None:

            print(
                f"新規プロジェクト: {title} "
                f"（通知なし）"
            )

            continue

        previous_likes = int(
            previous.get("likes", 0)
        )

        # ----------------------------------------------------
        # いいね増加
        # ----------------------------------------------------

        if current_likes > previous_likes:

            difference = (
                current_likes -
                previous_likes
            )

            print(
                f"いいね増加: {title} "
                f"{previous_likes} → "
                f"{current_likes} "
                f"(+{difference})"
            )

            created = create_like_notification(
                conn,
                user_id,
                title
            )

            if created:
                notification_count += 1

        # ----------------------------------------------------
        # 変化なし
        # ----------------------------------------------------

        elif current_likes == previous_likes:

            print(
                f"変化なし: {title} "
                f"{current_likes}"
            )

        # ----------------------------------------------------
        # いいね減少
        # ----------------------------------------------------

        else:

            print(
                f"いいね減少: {title} "
                f"{previous_likes} → "
                f"{current_likes}"
            )

    return notification_count


# ============================================================
# users.program_urls取得
# ============================================================

def get_users(conn):
    """
    usersテーブルから
    id / username / program_urls
    を取得
    """

    sql = """
        SELECT
            id,
            username,
            program_urls
        FROM users
        WHERE program_urls IS NOT NULL
    """

    with conn.cursor() as cur:

        cur.execute(sql)

        rows = cur.fetchall()

    return rows


# ============================================================
# users.program_views / program_likes 更新
# ============================================================

def update_user_program_stats(
    conn,
    user_id,
    projects
):
    """
    users.program_views
    users.program_likes

    を更新
    """

    views = [
        int(project.get("views", 0))
        for project in projects
    ]

    likes = [
        int(project.get("likes", 0))
        for project in projects
    ]

    sql = """
        UPDATE users
        SET
            program_views = %s,
            program_likes = %s
        WHERE id = %s
    """

    with conn.cursor() as cur:

        cur.execute(
            sql,
            (
                views,
                likes,
                user_id
            )
        )


# ============================================================
# メイン
# ============================================================

def main():

    print()
    print("=" * 70)
    print("Chanpro プロジェクト統計・いいね通知システム")
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # 前回JSON
    # --------------------------------------------------------

    previous_data = load_previous_stats()

    print(
        f"前回更新: "
        f"{previous_data.get('updated_at')}"
    )

    # --------------------------------------------------------
    # DB接続
    # --------------------------------------------------------

    print("PostgreSQLへ接続しています...")

    conn = psycopg.connect(
        DATABASE_URL
    )

    print("DB接続成功")

    # --------------------------------------------------------
    # users取得
    # --------------------------------------------------------

    users = get_users(conn)

    print(
        f"対象ユーザー数: {len(users)}"
    )

    # --------------------------------------------------------
    # URL重複除去
    # --------------------------------------------------------

    url_to_users = {}

    for user_id, username, program_urls in users:

        if not program_urls:
            continue

        for url in program_urls:

            if not url:
                continue

            url = url.strip()

            if not url:
                continue

            url_to_users.setdefault(
                url,
                []
            ).append(
                (
                    user_id,
                    username
                )
            )

    print(
        f"対象プロフィールURL数: "
        f"{len(url_to_users)}"
    )

    # --------------------------------------------------------
    # Playwright
    # --------------------------------------------------------

    new_profiles = {}

    total_notifications = 0

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            viewport={
                "width": 1440,
                "height": 1000
            }
        )

        # ----------------------------------------------------
        # URLごとに処理
        # ----------------------------------------------------

        for url, owners in url_to_users.items():

            print()
            print("#" * 70)
            print(f"処理中: {url}")
            print("#" * 70)

            current_profile = scrape_profile(
                page,
                url
            )

            # ------------------------------------------------
            # スクレイピング失敗
            # ------------------------------------------------

            if current_profile is None:

                print(
                    "取得失敗。前回データを保持します。"
                )

                if url in previous_data["profiles"]:

                    new_profiles[url] = (
                        previous_data["profiles"][url]
                    )

                continue

            # ------------------------------------------------
            # 前回プロフィール
            # ------------------------------------------------

            previous_profile = (
                previous_data["profiles"].get(url)
            )

            # ------------------------------------------------
            # このプロフィールを所有するユーザー
            # ------------------------------------------------

            for user_id, username in owners:

                print()
                print(
                    f"ユーザー: {username}"
                )

                # --------------------------------------------
                # いいね比較
                # --------------------------------------------

                notifications = compare_and_notify(
                    conn,
                    previous_profile,
                    current_profile,
                    user_id
                )

                total_notifications += notifications

                # --------------------------------------------
                # usersテーブル更新
                # --------------------------------------------

                update_user_program_stats(
                    conn,
                    user_id,
                    current_profile["projects"]
                )

            # ------------------------------------------------
            # JSON用データ
            # ------------------------------------------------

            new_profiles[url] = current_profile

        browser.close()

    # ========================================================
    # DBコミット
    # ========================================================

    conn.commit()

    print()
    print("=" * 70)
    print("DB更新完了")
    print(
        f"今回作成した通知: "
        f"{total_notifications}"
    )
    print("=" * 70)

    conn.close()

    # ========================================================
    # 新しいJSON
    # ========================================================

    new_data = {
        "updated_at": now_iso(),
        "profiles": new_profiles
    }

    save_stats(new_data)

    # ========================================================
    # Git差分確認
    # ========================================================

    print()
    print("=" * 70)
    print("GitHub更新")
    print("=" * 70)

    status = run_git(
        "status",
        "--short"
    )

    if not status:

        print(
            "変更がありません。"
        )

        return

    print()
    print("変更内容:")
    print(status)

    # --------------------------------------------------------
    # git add
    # --------------------------------------------------------

    run_git(
        "add",
        "program_stats.json"
    )

    # --------------------------------------------------------
    # git commit
    # --------------------------------------------------------

    commit_message = (
        "Update Chanpro program stats"
    )

    commit_result = subprocess.run(
        [
            "git",
            "commit",
            "-m",
            commit_message
        ],
        cwd=BASE_DIR,
        text=True,
        capture_output=True
    )

    if commit_result.returncode != 0:

        print("git commit失敗")

        print(
            commit_result.stdout
        )

        print(
            commit_result.stderr
        )

        raise RuntimeError(
            "git commitに失敗しました"
        )

    print(
        commit_result.stdout
    )

    # --------------------------------------------------------
    # git push
    # --------------------------------------------------------

    print("GitHubへpushしています...")

    push_result = subprocess.run(
        [
            "git",
            "push"
        ],
        cwd=BASE_DIR,
        text=True,
        capture_output=True
    )

    print(
        push_result.stdout
    )

    if push_result.returncode != 0:

        print(
            "git push失敗:"
        )

        print(
            push_result.stderr
        )

        raise RuntimeError(
            "git pushに失敗しました"
        )

    print()
    print("=" * 70)
    print("GitHub更新完了")
    print("=" * 70)


# ============================================================
# 実行
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print()
        print("中断されました。")

    except Exception as e:

        print()
        print("=" * 70)
        print("ERROR")
        print("=" * 70)
        print(e)

        raise
