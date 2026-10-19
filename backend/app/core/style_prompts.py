"""视觉风格系统级提示词映射模块（文件级说明）。

新建项目时选定的视觉风格（projects.style，中文标签）在此映射为全局强制的
"系统级 Style Prompt"：资产管理生成角色/场景/道具图片时，正向前缀（prefix）
注入最终提示词最前面，专属负面提示词（negative）注入尾部——不论画什么内容，
这组词都必须带上，确保画风不跑偏。

键与 NewProjectDialog 的 STYLES 九选项完全一致（中文字符串直比）；
projects.style 为空或不在表内时返回 None（不注入，行为回退）。
"""

from typing import Any, Dict, Optional


# 六风格 Style Prompt 映射表（prefix=系统级正向前缀；negative=专属负面提示词）
STYLE_PROMPTS: Dict[str, Dict[str, str]] = {
    "国风水墨": {
        "prefix": (
            "Traditional Chinese ink wash painting, elegant watercolor and ink style, "
            "xuan paper texture, expressive brush strokes, minimalist and ethereal "
            "atmosphere, masterpiece, high quality."
        ),
        "negative": (
            "3D render, photorealistic, western style, heavy oil painting, "
            "over-saturated colors, thick outlines."
        ),
    },
    "日系动漫": {
        "prefix": (
            "Japanese anime style, 2D illustration, cel shading, clean and crisp "
            "lineart, flat color, vibrant and bright lighting, highly detailed face, "
            "masterpiece, best quality."
        ),
        "negative": (
            "3D, realistic, photorealistic, creepy, deformed eyes, messy lines, "
            "heavy shadows, western comic."
        ),
    },
    "美式漫画": {
        "prefix": (
            "American comic book style, bold lineart, dynamic poses, high contrast, "
            "dramatic shading, halftone dots, vibrant and saturated colors, "
            "Marvel/DC style illustration, masterpiece."
        ),
        "negative": (
            "cute, chibi, pastel colors, delicate lines, anime style, soft lighting, "
            "flat shading, blurry."
        ),
    },
    "赛博朋克": {
        "prefix": (
            "Cyberpunk style, futuristic sci-fi, neon lights, high tech and low life, "
            "holographic projections, mechanical details, glowing neon signs, dark and "
            "gritty atmosphere, cinematic lighting, 8k resolution."
        ),
        "negative": (
            "traditional, historical, natural scenery, bright daylight, pastel colors, "
            "rural, primitive."
        ),
    },
    "写实厚涂": {
        "prefix": (
            "Semi-realistic thick impasto painting, visible brush strokes, oil painting "
            "texture, cinematic lighting, intricate details, rich and deep colors, "
            "dramatic shadows, concept art style, masterpiece."
        ),
        "negative": (
            "flat color, cel shading, cartoon, sketch, anime, simple lines, "
            "overexposed, low resolution."
        ),
    },
    "简约扁平": {
        "prefix": (
            "Minimalist flat illustration, vector art style, solid colors, geometric "
            "shapes, clean and smooth lines, no gradients, simple background, modern "
            "corporate illustration style, UI design style."
        ),
        "negative": (
            "realistic, 3D, heavy shading, detailed textures, intricate patterns, "
            "complex lighting, noisy."
        ),
    },
    # ---------- 真人写实系（三档）：实拍照片质感，与动漫系互斥 ----------
    # 影视级写实（电影质感）：光影深邃、胶片颗粒，适合悬疑/复仇/高品质短剧；
    # 画面控制词中普适的人像质感词（皮肤纹理/发丝/眼神聚焦）并入 prefix，
    # 场景相关词（光影氛围/表情）留给分镜描述层
    "影视级写实": {
        "prefix": (
            "Cinematic photography, photorealistic, ultra-realistic, shot on ARRI "
            "Alexa 65, 35mm lens, cinematic lighting, dramatic chiaroscuro, raw photo, "
            "8k resolution, film grain, depth of field, intricate details, detailed "
            "skin texture, realistic hair strands, sharp focus on eyes."
        ),
        "negative": (
            "cartoon, anime, 3d render, illustration, painting, plastic skin, "
            "overly smooth, CGI, deformed, cross-eyed, blurry, overexposed."
        ),
    },
    # 现代都市写实（高清短剧风）：干净明亮、高颜值微磨皮，适合霸总/甜宠/职场
    "现代都市写实": {
        "prefix": (
            "Ultra-realistic modern portrait photography, natural soft lighting, "
            "bright and clear, high fashion magazine style, professional makeup, "
            "symmetrical face, highly detailed eyes, commercial photography, 8k, "
            "sharp focus, flawless but realistic skin."
        ),
        "negative": (
            "dark lighting, horror, distorted face, heavy makeup, unnatural skin "
            "tone, oily skin, low resolution, anime, drawing, out of focus."
        ),
    },
    # 复古/年代写实：胶片色调（柯达金/富士绿）、年代质感，适合民国/80 90 年代怀旧
    "复古/年代写实": {
        "prefix": (
            "Vintage cinematic photography, retro film style, Kodak portra 400, "
            "muted warm tones, nostalgic atmosphere, realistic texture, historical "
            "setting, soft vintage lighting, authentic details, film grain, "
            "authentic facial features."
        ),
        "negative": (
            "modern, futuristic, neon lights, overly clean, plastic, HDR, "
            "oversaturated, anime, digital art."
        ),
    },
}


def get_style_prompts(style: Optional[str]) -> Optional[Dict[str, str]]:
    """按项目视觉风格标签取 Style Prompt（系统级前缀与负面词）。

    Args:
        style: 项目视觉风格标签（projects.style，如「日系动漫」；可空）。

    Returns:
        Optional[Dict[str, str]]: {"prefix": ..., "negative": ...}；风格为空或
            不在映射表内返回 None（生成时不注入，行为回退）。

    Raises:
        无：纯字典查询。
    """
    key = str(style or "").strip()
    if not key:
        return None
    return STYLE_PROMPTS.get(key)
