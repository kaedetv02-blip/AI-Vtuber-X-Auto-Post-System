"""AI VTuber のトレンド連動 X 自動投稿スクリプト。

GitHub Actions の Secrets を環境変数として渡して実行することを想定しています。
必要な環境変数:
  GEMINI_API_KEY, ANTHROPIC_API_KEY,
  CHAR1_X_API_KEY, CHAR1_X_API_SECRET,
  CHAR1_X_ACCESS_TOKEN, CHAR1_X_ACCESS_TOKEN_SECRET,
  CHAR2_X_API_KEY, CHAR2_X_API_SECRET,
  CHAR2_X_ACCESS_TOKEN, CHAR2_X_ACCESS_TOKEN_SECRET

任意の環境変数:
  GEMINI_TEXT_MODEL (既定: gemini-3.5-flash)
  CLAUDE_MODEL (既定: claude-fable-5)
  TWITTREND_URL (既定: https://twittrend.jp/)
  POST_DELAY_MIN_SECONDS / POST_DELAY_MAX_SECONDS (既定: 90 / 180)
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic
import pytz
import requests
import tweepy
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from PIL import Image


# GitHub Actions ではリポジトリ内に置く提供済みの参照画像を使います。
CHAR1_IMAGE_PATH = "assets/kuroe_reference.png"
CHAR2_IMAGE_PATH = "assets/ruru_reference.png"
# Each supplied image is a character design sheet containing multiple angles.  Keep
# this as a tuple because the Gemini API accepts one or more reference images.
CHAR1_IMAGE_PATHS = (CHAR1_IMAGE_PATH,)
CHAR2_IMAGE_PATHS = (CHAR2_IMAGE_PATH,)

# Nano Banana Pro: prioritize exact character identity and instruction following.
GEMINI_IMAGE_MODEL = "gemini-3-pro-image"
JST = pytz.timezone("Asia/Tokyo")
LOGGER = logging.getLogger("ai_vtuber_sns")
POST_THEMES = (
    "おはようポスト",
    "ネタポスト（面白・日常）",
    "おやすみポスト",
)
MAX_TWEET_BODY_CHARACTERS = 100
HASHTAG_PATTERN = re.compile(r"#([^\s#]+)")
EMOJI_PATTERN = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
KAOMOJI_PATTERN = re.compile(r"[\(（][^\n()（）]{1,24}[\)）]")


@dataclass(frozen=True)
class Character:
    """投稿に必要なキャラクター設定。人格は運用方針に合わせて調整可能。"""

    key: str
    name: str
    image_paths: tuple[str, ...]
    x_env_prefix: str
    persona: str
    visual_direction: str
    scene_variations: tuple[str, ...]
    post_emoji: str
    post_kaomoji: str
    trend_interests: str


CHARACTERS = (
    Character(
        key="char1",
        name="クロエ",
        image_paths=CHAR1_IMAGE_PATHS,
        x_env_prefix="CHAR1",
        post_emoji="🔬",
        post_kaomoji="( ˘ᵕ˘ )",
        trend_interests=(
            "science, medicine, technology, space, natural phenomena, animal behavior, research, "
            "calm mysteries, puzzles, video games, anime, and intellectual games. Avoid political conflict, scandals, "
            "tragedies, and hostile controversies."
        ),
        visual_direction=(
            "A serious, composed researcher expression with calm focused eyes and only the faintest "
            "reserved smile; direct or side-glancing eye contact; one gloved hand thoughtfully near her "
            "chin, adjusting her lab-coat collar, or holding a small research note. Use an off-center "
            "close-up, cool green laboratory accent light, precise posture, and quiet intellectual energy."
        ),
        scene_variations=(
            "at a laboratory workbench, reviewing a glowing sample vial",
            "in a quiet game room, holding a controller with a subtle competitive look",
            "by a book-lined window, annotating a research notebook",
            "in a planetarium corridor, looking up at a projected star map",
            "at a cozy anime screening desk, adjusting her headphones",
            "in a rain-speckled laboratory window reflection, holding a small specimen case",
            "on a rooftop observatory at blue hour, checking a compact field instrument",
        ),
        persona=(
            "黒髪・赤い瞳の猫耳研究者。真面目で冷静、理知的で観察眼が鋭い。"
            "意外とゲームとアニメが好きで、好きな作品の話題では少しだけ熱が入る。"
            "簡潔で落ち着いた日本語を使い、根拠のない断定を避け、研究者らしい洞察を添える。"
        ),
    ),
    Character(
        key="char2",
        name="ルル",
        image_paths=CHAR2_IMAGE_PATHS,
        x_env_prefix="CHAR2",
        post_emoji="🎀",
        post_kaomoji="(՞ ᴗ ̫ ᴗ՞)",
        trend_interests=(
            "cute jirai-kei fashion, idols, music, anime, otaku culture, sweets, cafes, romance, "
            "cute animals, and lighthearted internet culture. Avoid political conflict, scandals, "
            "tragedies, and hostile controversies."
        ),
        visual_direction=(
            "An endearingly airheaded, sweet jirai-kei girl expression: wide-eyed surprise, a dreamy "
            "soft smile, or a tiny delighted gasp; gaze drifting slightly away or upward; one delicate "
            "hand near her lips, cheek, ribbon, or heart-level chest gesture. Use a three-quarter close-up, "
            "dreamy pink-purple rim light, black-and-pink girly gothic accents, playful sleeve movement, "
            "and an innocent whimsical mood."
        ),
        scene_variations=(
            "in a dreamy pink-lit cafe, cradling a tiny decorated dessert",
            "at a bedroom vanity, gently fixing one of her ribbons",
            "outside a cute arcade, reacting with delighted surprise to a prize plush",
            "at an idol merchandise display, clasping her hands with sparkling excitement",
            "under a softly glowing night-city sign, holding a heart-shaped compact",
            "at a pastel tea table, peeking over a small handwritten note",
            "in a flower-filled boutique corner, lifting one sleeve in a shy wave",
        ),
        persona=(
            "黒髪・紫の瞳の猫耳地雷系娘。天然で愛嬌があり、少し抜けているが人を惹きつける。"
            "ふわっとやわらかな日本語で、小さな驚きや素直な感想を交え、トレンドを自然に話題へ添える。"
        ),
    ),
)


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def require_environment() -> dict[str, str]:
    """必要なシークレットを検証して返す。値そのものはログに出さない。"""
    names = (
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CHAR1_X_API_KEY",
        "CHAR1_X_API_SECRET",
        "CHAR1_X_ACCESS_TOKEN",
        "CHAR1_X_ACCESS_TOKEN_SECRET",
        "CHAR2_X_API_KEY",
        "CHAR2_X_API_SECRET",
        "CHAR2_X_ACCESS_TOKEN",
        "CHAR2_X_ACCESS_TOKEN_SECRET",
    )
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"必須の環境変数がありません: {', '.join(missing)}")
    return values


def get_post_theme(now_jst: datetime) -> str | None:
    """投稿テーマを返す。FORCE_THEME があれば時間帯より優先する。"""
    forced_theme = os.environ.get("FORCE_THEME", "").strip()
    forced_theme = {
        "morning": POST_THEMES[0],
        "fun": POST_THEMES[1],
        "night": POST_THEMES[2],
    }.get(forced_theme, forced_theme)
    if forced_theme and forced_theme != "auto":
        if forced_theme not in POST_THEMES:
            allowed_themes = ", ".join(POST_THEMES)
            raise ValueError(
                f"FORCE_THEME が不正です: {forced_theme}。指定できる値: {allowed_themes}"
            )
        return forced_theme

    hour = now_jst.hour
    if 5 <= hour < 10:
        return "おはようポスト"
    if 15 <= hour < 18:
        return "ネタポスト（面白・日常）"
    if 20 <= hour < 24:
        return "おやすみポスト"
    return None


def get_target_characters() -> tuple[Character, ...]:
    """Return the characters selected for this invocation."""
    target = os.environ.get("TARGET_CHARACTER", "all").strip().lower()
    if target in {"", "all"}:
        return CHARACTERS

    selected = tuple(character for character in CHARACTERS if character.key == target)
    if not selected:
        allowed = ", ".join(["all", *(character.key for character in CHARACTERS)])
        raise ValueError(f"TARGET_CHARACTER must be one of: {allowed}")
    return selected


def clean_trend_candidate(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"^\d+\s*[.．、:]\s*", "", value)
    return value.strip(" ・　")


def fetch_trend_candidates() -> list[str]:
    """Twittrend の「現在」欄からキャラクター選定用の候補を取得する。"""
    url = os.environ.get("TWITTREND_URL", "https://twittrend.jp/")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; AIVTuberTrendBot/1.0; "
            "+https://github.com/)"
        ),
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    }
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Twittrend の取得に失敗しました: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    current_heading = soup.find(
        lambda tag: tag.name in {"h2", "h3", "h4"}
        and clean_trend_candidate(tag.get_text(" ", strip=True)) == "現在"
    )

    candidates: list[str] = []
    if current_heading:
        # 「現在」以降、次の時間帯見出しまでのリスト項目を優先する。
        for element in current_heading.find_all_next(["li", "h2", "h3", "h4"], limit=100):
            if element is not current_heading and element.name in {"h2", "h3", "h4"}:
                break
            if element.name == "li":
                text = clean_trend_candidate(element.get_text(" ", strip=True))
                if text:
                    candidates.append(text)

    # サイトのHTML変更に備えたフォールバック。/trend/ を指すリンクを利用する。
    if not candidates:
        for anchor in soup.select('a[href*="/trend/"]'):
            text = clean_trend_candidate(anchor.get_text(" ", strip=True))
            if text:
                candidates.append(text)

    # 重複、順位だけの文字列、明らかなナビゲーションを除去する。
    ignored = {"日本", "世界", "現在", "過去のトレンド", "日本のトレンド"}
    unique_candidates: list[str] = []
    for candidate in candidates:
        if candidate in ignored or candidate.isdigit() or candidate in unique_candidates:
            continue
        if len(candidate) <= 80:
            unique_candidates.append(candidate)

    if not unique_candidates:
        raise RuntimeError("Twittrend のHTMLからトレンドワードを抽出できませんでした")

    trend_candidates = unique_candidates[:20]
    LOGGER.info("取得したトレンド候補数: %s", len(trend_candidates))
    return trend_candidates


def summarize_trend(gemini_client: genai.Client, trend_word: str) -> str:
    """Google Search Grounding を有効にして、トレンドの背景を簡潔に調べる。"""
    # 提供終了した Gemini 1.5 / 2.5 Flash の代わりに、
    # Google Search Grounding 対応の安定版を使う。
    model = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.5-flash")
    prompt = (
        f"Xで話題のトレンドワード「{trend_word}」について調べてください。\n"
        "このトレンドが何について話題になっているかを、日本語でちょうど3行、"
        "各行は簡潔に要約してください。未確認情報は断定しないでください。"
    )
    try:
        response = gemini_client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                thinking_config=types.ThinkingConfig(thinking_level="low"),
            ),
        )
        summary = (getattr(response, "text", None) or "").strip()
    except Exception as exc:
        raise RuntimeError(f"Gemini によるトレンド情報収集に失敗しました: {exc}") from exc

    if not summary:
        raise RuntimeError("Gemini からトレンド要約テキストが返りませんでした")
    LOGGER.info("Gemini のトレンド要約を取得しました")
    return summary


def extract_json_object(text: str) -> dict[str, Any]:
    """Claude の応答から、コードフェンスの有無を問わず JSON オブジェクトを取り出す。"""
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise RuntimeError("Claude の応答に JSON オブジェクトがありません")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Claude の JSON を解析できません: {exc}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("Claude の JSON 応答がオブジェクトではありません")
    return parsed


def choose_trend_for_character(
    claude_client: anthropic.Anthropic,
    character: Character,
    trend_candidates: list[str],
    used_trends: set[str],
) -> str | None:
    """Pick one suitable unused trend for a character, or skip when none fits."""
    model = os.environ.get("CLAUDE_MODEL", "claude-fable-5")
    available_candidates = [
        candidate for candidate in trend_candidates if candidate not in used_trends
    ]
    if not available_candidates:
        return None

    prompt = f"""
Choose an X trend for the AI VTuber below.

Character: {character.name}
Character personality: {character.persona}
Topics this character genuinely likes: {character.trend_interests}
Allowed trend candidates (choose only an exact item from this list):
{json.dumps(available_candidates, ensure_ascii=False)}

Return only this JSON object:
{{"trend": "exact candidate"}}

If none of the allowed candidates genuinely fit the character's interests and safe tone, return:
{{"trend": null}}

Never choose political conflict, scandal, tragedy, harassment, or a topic the character would not enjoy.
Do not invent, rewrite, or combine candidates.
""".strip()
    try:
        message = claude_client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        raise RuntimeError(f"{character.name} のトレンド選定に失敗しました: {exc}") from exc

    response_text = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )
    payload = extract_json_object(response_text)
    selected = payload.get("trend")
    if selected is None:
        return None
    selected = str(selected).strip()
    if selected in available_candidates:
        LOGGER.info("%s が選んだトレンド: %s", character.name, selected)
        return selected
    LOGGER.warning("%s のトレンド選定が候補外だったためスキップします: %s", character.name, selected)
    return None


def normalize_hashtag(value: str) -> str:
    """Return one whitespace-free hashtag, or an empty string."""
    body = re.sub(r"\s+", "", value.lstrip("#")).strip(".,!?:;。！？、")
    return f"#{body}" if body else ""


def add_readable_line_break(body: str) -> str:
    """Put every completed sentence in its own readable paragraph."""
    lines: list[str] = []
    for source_line in body.splitlines():
        sentence = ""
        for character in source_line:
            sentence += character
            if character in "。！？!?":
                if sentence.strip():
                    lines.append(sentence.strip())
                sentence = ""
        if sentence.strip():
            lines.append(sentence.strip())
    return "\n\n".join(lines)


def format_tweet(tweet: str, trend_word: str, character: Character) -> str:
    """Keep posts brief and readable, with all hashtags on the final line."""
    hashtags: list[str] = []
    for value in [trend_word, *HASHTAG_PATTERN.findall(tweet)]:
        hashtag = normalize_hashtag(value)
        if hashtag and hashtag not in hashtags:
            hashtags.append(hashtag)
    hashtags = hashtags[:3]
    if not hashtags:
        raise RuntimeError("投稿用ハッシュタグを作成できませんでした")

    without_hashtags = HASHTAG_PATTERN.sub("", tweet)
    lines = [
        re.sub(r"[ \t]+", " ", line).strip()
        for line in without_hashtags.splitlines()
    ]
    body = "\n".join(line for line in lines if line)
    if not body:
        raise RuntimeError("ハッシュタグ以外の投稿本文がありません")

    body = body[:MAX_TWEET_BODY_CHARACTERS].rstrip()
    body = add_readable_line_break(body)
    tag_line = " ".join(hashtags)
    available_body_length = 140 - len(tag_line) - 2
    if available_body_length < 2:
        raise RuntimeError("投稿用ハッシュタグが長すぎます")
    body = body[:available_body_length].rstrip()

    additions: list[str] = []
    if not EMOJI_PATTERN.search(body):
        additions.append(character.post_emoji)
    if not KAOMOJI_PATTERN.search(body):
        additions.append(character.post_kaomoji)
    if additions:
        suffix = f" {' '.join(additions)}"
        body = f"{body[:max(1, available_body_length - len(suffix))].rstrip()}{suffix}"

    return f"{body}\n\n{tag_line}"


def make_character_content(
    claude_client: anthropic.Anthropic,
    character: Character,
    theme: str,
    trend_word: str,
    trend_summary: str,
) -> tuple[str, str]:
    """Claude に投稿本文と英語の画像プロンプトを作らせる。"""
    model = os.environ.get("CLAUDE_MODEL", "claude-fable-5")
    scene_variation = random.choice(character.scene_variations)
    instructions = f"""
あなたはAI VTuber「{character.name}」のSNS編集者です。
キャラクター設定: {character.persona}
今回の投稿テーマ: {theme}
トレンドワード: {trend_word}
トレンドの要約:
{trend_summary}

次のJSONオブジェクトだけを返してください。Markdownのコードフェンスや説明文は不要です。
{{
  "tweet": "日本語のX投稿本文。140文字以内。トレンドへの言及は自然で控えめにし、事実不明なことを断定しない。URLは含めない。",
  "image_prompt": "投稿テーマとトレンドの雰囲気に合う、英語だけの具体的な画像生成プロンプト。人物名・固有の著作物・画面上の文字・ロゴ・透かしは含めない。"
}}
""".strip()
    instructions += "\n\n" + """

Non-negotiable quality rules:
- Let the character profile control the Japanese wording, emotional reaction, and point of view.
  Do not write a generic interchangeable influencer post.
- Write the tweet body as short Japanese sentences (about 100 characters or fewer before hashtags),
  with one or two fitting emojis and one natural kaomoji. Put every completed sentence in its own paragraph, with one blank line
  after "。", "！", or "？". Never break a sentence in the middle just to make more lines.
- Do not include hashtags in the body. Put one to three relevant hashtags only on a separate final line.
- The image_prompt must be detailed English only and must describe this specific character's
  established personality, wardrobe, and mood rather than a generic anime girl.
- Keep image_prompt under 150 words and on one JSON string line.
- It must specify a visible facial expression, eye direction, and a meaningful hand gesture or pose.
- Avoid a centered, neutral, front-facing portrait. Give the character a distinctive emotional beat,
  body orientation, eye line, and composition that do not resemble the other character.
""".strip()
    instructions += f"""

Character-specific visual direction (must be reflected in image_prompt):
{character.visual_direction}

Required scene variation for this post (must be reflected in image_prompt):
{scene_variation}
""".strip()

    payload: dict[str, Any] | None = None
    for attempt in range(1, 3):
        try:
            message = claude_client.messages.create(
                model=model,
                max_tokens=1000,
                messages=[{"role": "user", "content": instructions}],
            )
            response_text = "".join(
                block.text for block in message.content if getattr(block, "type", "") == "text"
            )
            payload = extract_json_object(response_text)
            break
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(
                    f"Claude による {character.name} の投稿生成に失敗しました: {exc}"
                ) from exc
            LOGGER.warning(
                "%s のClaude応答を解析できなかったため再試行します (%s/2)",
                character.name,
                attempt,
            )

    if payload is None:
        raise RuntimeError(f"Claude の {character.name} 向け応答を取得できませんでした")
    tweet = str(payload.get("tweet", "")).strip()
    image_prompt = str(payload.get("image_prompt", "")).strip()

    if not tweet or not image_prompt:
        raise RuntimeError(f"Claude の {character.name} 向け応答に tweet または image_prompt がありません")
    tweet = format_tweet(tweet, trend_word, character)

    # 参照画像を渡すため、同一人物らしさとテキスト無しを明示する。
    image_prompt = (
        f"{image_prompt}\n\n"
        "The supplied reference image is this character's canonical multi-view character-design sheet. "
        "Use it as the single source of truth: preserve the exact face, eye color, hairstyle, cat ears, "
        "body proportions, signature outfit, accessories, color palette, and polished anime line-art and "
        "soft-shaded illustration style. Do not redesign, age up, or substitute the character. "
        "Create one finished scene, never a turnaround sheet, collage, split panel, reference board, or "
        "white-background copy of the reference. Create a vertical 3:4 social-media illustration: a close-up "
        "portrait (head-and-shoulders or chest-up) with a clear, intentional Dutch-angle camera tilt. "
        "Make the facial expression, eye direction, and one meaningful hand gesture clearly visible. "
        f"Required scene variation: {scene_variation}. "
        f"Character-specific visual direction: {character.visual_direction} "
        "Vary the setting, prop, camera distance, emotional beat, and pose from prior posts while preserving "
        "the same character-design and art-style identity. Avoid a neutral centered front-facing pose or a "
        "generic composition; keep this character's composition distinct from the other character. "
        "No text, letters, logos, watermarks, or extra characters."
    )
    return tweet, image_prompt


def generate_image(
    gemini_client: genai.Client, character: Character, image_prompt: str
) -> Path:
    """参照画像と英語プロンプトを Gemini 画像モデルへ同時に渡して一枚保存する。"""
    reference_paths = tuple(Path(path) for path in character.image_paths)
    missing_paths = [path for path in reference_paths if not path.is_file()]
    if missing_paths:
        raise RuntimeError(
            f"{character.name} の参照画像が見つかりません: {', '.join(map(str, missing_paths))}"
        )

    output_path = Path(tempfile.gettempdir()) / f"temp_{character.key}_{uuid.uuid4().hex}.png"
    try:
        max_attempts = int(os.environ.get("IMAGE_GENERATION_MAX_ATTEMPTS", "2"))
    except ValueError:
        max_attempts = 2
    max_attempts = max(1, min(max_attempts, 3))

    try:
        reference_images: list[Image.Image] = []
        for reference_path in reference_paths:
            with Image.open(reference_path) as source_image:
                reference_images.append(source_image.convert("RGBA"))

        try:
            for attempt in range(1, max_attempts + 1):
                response = gemini_client.models.generate_content(
                    model=GEMINI_IMAGE_MODEL,
                    contents=[image_prompt, *reference_images],
                    config=types.GenerateContentConfig(
                        response_modalities=["TEXT", "IMAGE"],
                        image_config=types.ImageConfig(
                            aspect_ratio="3:4", image_size="1K"
                        ),
                    ),
                )
                parts = getattr(response, "parts", None) or []
                for part in parts:
                    if getattr(part, "inline_data", None) is not None:
                        part.as_image().save(output_path)
                        LOGGER.info("%s の生成画像を保存しました: %s", character.name, output_path)
                        return output_path
                if attempt < max_attempts:
                    LOGGER.warning(
                        "%s の画像応答に画像データがなかったため、再試行します (%s/%s)",
                        character.name,
                        attempt,
                        max_attempts,
                    )
        finally:
            for reference_image in reference_images:
                reference_image.close()
        raise RuntimeError("Gemini の画像生成レスポンスに画像データがありません")
    except Exception as exc:
        # 途中まで生成された空ファイルも残さない。
        if output_path.exists():
            os.remove(output_path)
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Gemini による {character.name} の画像生成に失敗しました: {exc}") from exc


def create_x_clients(
    credentials: dict[str, str], character: Character
) -> tuple[tweepy.API, tweepy.Client]:
    """対象キャラクターのアカウント用に、X APIクライアントを作成する。"""
    api_key = credentials[f"{character.x_env_prefix}_X_API_KEY"]
    api_secret = credentials[f"{character.x_env_prefix}_X_API_SECRET"]
    access_token = credentials[f"{character.x_env_prefix}_X_ACCESS_TOKEN"]
    access_token_secret = credentials[f"{character.x_env_prefix}_X_ACCESS_TOKEN_SECRET"]
    auth = tweepy.OAuth1UserHandler(
        api_key,
        api_secret,
        access_token,
        access_token_secret,
    )
    media_api = tweepy.API(auth, wait_on_rate_limit=True)
    post_client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
        wait_on_rate_limit=True,
    )
    return media_api, post_client


def post_to_x(media_api: tweepy.API, post_client: tweepy.Client, tweet: str, image_path: Path) -> None:
    """画像をアップロードしてから、本文とともに X API v2 へ投稿する。"""
    try:
        media = media_api.media_upload(filename=str(image_path))
        post_client.create_tweet(text=tweet, media_ids=[media.media_id_string])
    except tweepy.TweepyException as exc:
        raise RuntimeError(f"X への投稿に失敗しました: {exc}") from exc


def run_character(
    character: Character,
    theme: str,
    trend_word: str,
    trend_summary: str,
    claude_client: anthropic.Anthropic,
    gemini_client: genai.Client,
    credentials: dict[str, str],
) -> bool:
    """一人分を実行し、成功/失敗を返す。一時画像は必ず削除する。"""
    image_path: Path | None = None
    try:
        media_api, post_client = create_x_clients(credentials, character)
        tweet, image_prompt = make_character_content(
            claude_client, character, theme, trend_word, trend_summary
        )
        LOGGER.info("%s の投稿本文を生成しました: %s", character.name, tweet)
        image_path = generate_image(gemini_client, character, image_prompt)
        post_to_x(media_api, post_client, tweet, image_path)
        LOGGER.info("%s のX投稿が完了しました", character.name)
        return True
    except Exception:
        LOGGER.exception("%s の処理中にエラーが発生しました", character.name)
        return False
    finally:
        if image_path and image_path.exists():
            try:
                os.remove(image_path)
                LOGGER.info("一時画像を削除しました: %s", image_path)
            except OSError:
                LOGGER.exception("一時画像を削除できませんでした: %s", image_path)


def main() -> int:
    configure_logging()
    now_jst = datetime.now(JST)
    LOGGER.info("現在の日本時間: %s", now_jst.strftime("%Y-%m-%d %H:%M:%S %Z"))
    try:
        theme = get_post_theme(now_jst)
    except ValueError as exc:
        LOGGER.error("投稿テーマの設定が不正です: %s", exc)
        return 1

    forced_theme = os.environ.get("FORCE_THEME", "").strip()
    if forced_theme and forced_theme != "auto":
        LOGGER.warning("FORCE_THEME により時間帯判定を上書きしています: %s", theme)
    if theme is None:
        LOGGER.info("投稿対象外の時間帯です。処理を終了します")
        return 0
    LOGGER.info("今回の投稿テーマ: %s", theme)

    try:
        credentials = require_environment()
        trend_candidates = fetch_trend_candidates()
        gemini_client = genai.Client(api_key=credentials["GEMINI_API_KEY"])
        claude_client = anthropic.Anthropic(api_key=credentials["ANTHROPIC_API_KEY"])
    except Exception:
        LOGGER.exception("投稿の事前準備に失敗しました")
        return 1

    try:
        target_characters = get_target_characters()
    except ValueError as exc:
        LOGGER.error("投稿対象の設定が不正です: %s", exc)
        return 1

    used_trends: set[str] = set()
    trend_summaries: dict[str, str] = {}
    character_posts: list[tuple[Character, str, str]] = []
    selection_errors = 0
    for character in target_characters:
        try:
            trend_word = choose_trend_for_character(
                claude_client, character, trend_candidates, used_trends
            )
            if trend_word is None:
                LOGGER.info("%s に合うトレンドがないため今回の投稿をスキップします", character.name)
                continue
            if trend_word not in trend_summaries:
                trend_summaries[trend_word] = summarize_trend(gemini_client, trend_word)
            character_posts.append((character, trend_word, trend_summaries[trend_word]))
            used_trends.add(trend_word)
        except Exception:
            selection_errors += 1
            LOGGER.exception("%s 向けトレンドの選定または要約に失敗しました", character.name)

    if not character_posts:
        if selection_errors:
            LOGGER.error("投稿対象トレンドを準備できませんでした")
            return 1
        LOGGER.info("今回の候補には、どちらのキャラクターにも合う話題がありませんでした")
        return 0

    successes = 0
    for index, (character, trend_word, trend_summary) in enumerate(character_posts):
        posted = run_character(
            character,
            theme,
            trend_word,
            trend_summary,
            claude_client,
            gemini_client,
            credentials,
        )
        if posted:
            successes += 1

        # 一人目の投稿が完了した後だけ、二人目までランダムに待機する。
        if posted and index < len(character_posts) - 1:
            minimum = int(os.environ.get("POST_DELAY_MIN_SECONDS", "90"))
            maximum = int(os.environ.get("POST_DELAY_MAX_SECONDS", "180"))
            if minimum < 0 or maximum < minimum:
                LOGGER.warning("投稿待機時間の設定が不正なため、90〜180秒を使用します")
                minimum, maximum = 90, 180
            delay = random.randint(minimum, maximum)
            LOGGER.info("スパム判定を避けるため、次の投稿まで%s秒待機します", delay)
            time.sleep(delay)

    if successes == len(character_posts) and not selection_errors:
        LOGGER.info("全キャラクターの投稿が完了しました")
        return 0
    LOGGER.error(
        "%s/%s 人の投稿に失敗しました",
        len(character_posts) - successes + selection_errors,
        len(character_posts) + selection_errors,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
