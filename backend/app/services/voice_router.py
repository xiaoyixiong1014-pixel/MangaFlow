"""角色音色路由模块（文件级说明）。

把角色圣经的「音色设定」（voice_note）与「性别」（gender）路由为业务音色
标识（male_01/male_02/female_01/female_02），供配音（TTS）任务创建时按
角色自动选音色——改变此前全项目共用固定缺省音色（female_01）、角色性格
气质无法体现在配音上的问题。

路由规则（关键词匹配，宽松命中）：
1. 性别判定优先 gender 列（男性/女性/中性），gender 缺失时从 voice_note
   的性别关键词推断（男/叔/爷/兄 → 男声；女/妈/姨/姐 → 女声）；
2. 男声组：「少年/顽皮/活泼/机灵/逗趣/元气」→ male_02（顽皮少年音），
   其余 → male_01（清晰自然男声）；
3. 女声组：「清亮/元气/可爱/少女/甜/电子/系统」→ female_02（清亮女声），
   其余 → female_01（多语言女声）；
4. 无任何命中（voice_note 与 gender 均缺）→ 返回 fallback（调用方缺省）。

工具无关：返回的是**业务音色标识**，渠道专属音色 ID 的映射仍由各 TTS
provider（如 tts_bailian._VOICE_MAP）承担，与既有链路解耦。
"""

from typing import Optional

# 男声组关键词
_MALE_WORDS = ("男", "叔", "爷", "伯", "兄", "先生", "老者", "爸", "爹")
# 男声组中偏「顽皮少年」气质的关键词（→ male_02）
_MALE_PLAYFUL_WORDS = ("少年", "顽皮", "活泼", "机灵", "逗趣", "跳脱")
# 女声组中偏「清亮元气」气质的关键词（→ female_02）
_FEMALE_BRIGHT_WORDS = ("清亮", "元气", "可爱", "少女", "甜", "电子", "系统", "机灵")
# 女声组关键词（gender 缺失时从 voice_note 推断）
_FEMALE_WORDS = ("女", "妈", "姨", "姐", "妹", "娘", "奶奶", "婶")


def resolve_voice(
    voice_note: Optional[str],
    gender: Optional[str] = None,
    fallback: str = "female_01",
) -> str:
    """按角色音色设定与性别路由业务音色标识。

    Args:
        voice_note: 角色音色设定文本（如「青年男声，低沉磁性，语速偏慢」；
            可空）。
        gender: 角色性别（男性/女性/中性；可空）。
        fallback: 无任何路由依据时的兜底音色标识。

    Returns:
        str: 业务音色标识（male_01/male_02/female_01/female_02 之一）。

    Raises:
        无：纯关键词匹配，文本异常按缺省处理。
    """
    note = str(voice_note or "")
    gender_text = str(gender or "")
    combined = f"{gender_text}；{note}"

    # 1) 性别判定：gender 列优先，缺失时从 voice_note 关键词推断
    is_male = "男" in gender_text
    is_female = "女" in gender_text
    if not is_male and not is_female:
        is_male = any(w in combined for w in _MALE_WORDS)
        is_female = (not is_male) and any(w in combined for w in _FEMALE_WORDS)

    # 2) 组内按气质特质细分
    if is_male:
        return (
            "male_02"
            if any(w in note for w in _MALE_PLAYFUL_WORDS)
            else "male_01"
        )
    if is_female:
        return (
            "female_02"
            if any(w in note for w in _FEMALE_BRIGHT_WORDS)
            else "female_01"
        )
    return fallback


# 语速推导关键词（voice_note 说话习惯段 → TTS speech_rate 倍率）
_SLOW_WORDS = ("缓慢", "慢慢", "慢条斯理", "语速偏慢", "语速慢", "慢吞吞", "拖长", "有气无力")
_FAST_WORDS = ("语速偏快", "语速快", "急促", "干脆", "雷厉风行", "快言快语", "利落")
# 音调推导关键词（voice_note 音色质地段 → TTS pitch_rate 倍率）
_DEEP_WORDS = ("低沉", "浑厚", "沧桑", "沙哑", "磁性", "沉稳", "厚重")
_SHARP_WORDS = ("尖细", "清亮", "高亢", "清脆", "甜美", "明亮")


def resolve_speech_params(
    voice_note: Optional[str],
    fallback_speed: float = 1.0,
    fallback_pitch: float = 1.0,
) -> "tuple[float, float]":
    """从角色音色设定文本推导语速/音调倍率（TTS 表现力参数化）。

    业务音色库仅 4 个枚举音色，voice_note 中的「语速缓慢/低沉磁性/清亮」等
    质地描述由本函数转为 speech_rate/pitch_rate 参数（每类取首个命中关键
    词，未命中回退调用方缺省）。

    Args:
        voice_note: 角色音色设定文本（可空）。
        fallback_speed: 无命中时的语速回退值。
        fallback_pitch: 无命中时的音调回退值。

    Returns:
        tuple[float, float]: (speech_rate, pitch_rate)（语速 0.5~2.0、音调 0.5~2.0）。

    Raises:
        无：纯关键词匹配，文本异常按缺省处理。
    """
    note = str(voice_note or "")
    speed = next(
        (v for w, v in ((w, 0.85) for w in _SLOW_WORDS) if w in note),
        next((v for w, v in ((w, 1.15) for w in _FAST_WORDS) if w in note), fallback_speed),
    )
    pitch = next(
        (v for w, v in ((w, 0.85) for w in _DEEP_WORDS) if w in note),
        next((v for w, v in ((w, 1.15) for w in _SHARP_WORDS) if w in note), fallback_pitch),
    )
    return (speed, pitch)
