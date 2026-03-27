from __future__ import annotations

from typing import Dict, List


LOOK_PRESETS: Dict[str, Dict[str, object]] = {
    "warm_family_vlog": {
        "label": "Warm Family Vlog",
        "category": "preview+final",
        "preview_prompt": """Act as a senior video editor and colorist creating a preview version of a YouTube family travel vlog.

Create a preview-grade version of this footage with a warm, emotional, soft cinematic vlog style. The goal is to make family moments feel intimate, beautiful, natural, and memory-driven without looking overly dramatic or artificial.

Preview goals:
- Prioritize warm skin tones, gentle contrast, soft highlight roll-off, and emotional natural color.
- Make the image feel cozy, heartfelt, and polished.
- Keep brightness comfortable and inviting, not dark or moody.
- Gently clean noise, compression artifacts, flicker, and uneven color.
- Improve clarity carefully without harsh sharpening or fake texture.
- Preserve realistic faces and authentic family expressions.
- Make sunset light, indoor ambient light, and candid family moments feel especially soft and memorable.
- Keep colors rich but restrained, with a premium cinematic YouTube finish.
- Match shots consistently across indoor and outdoor scenes.

Important:
This is a preview pass only.
Do not over-process the footage.
Do not use aggressive contrast, heavy teal-orange grading, fake HDR, or over-smoothed skin.
Preserve realism, natural motion, and the authenticity of family memories.

Creative direction:
warm family memories, soft cinematic vlog, emotional travel film, gentle contrast, natural skin tones, cozy light, polished but authentic YouTube finish

Generate a preview using representative shots that include faces, outdoor scenery, indoor scenes, and moving travel moments so the look can be reviewed before final mastering.""",
        "final_prompt": """Act as a senior finishing colorist and post-production specialist completing the final master of a YouTube family travel vlog.

Use the warm emotional cinematic preview style as the approved base look and produce the final polished version. Refine the footage so it feels heartfelt, premium, natural, and beautifully consistent across the full video.

Final goals:
- Preserve the approved warm emotional tone while improving consistency, polish, and shot-to-shot balance.
- Keep skin tones flattering, natural, and clean.
- Refine white balance, exposure, contrast, and highlight roll-off across all scenes.
- Make family moments feel intimate, soft, and memorable without losing realism.
- Improve detail in faces, clothing, skies, and backgrounds while avoiding oversharpening.
- Reduce noise and artifacts subtly while preserving natural texture.
- Ensure indoor, outdoor, golden-hour, and night shots feel visually unified.
- Maintain a bright, cozy, premium YouTube vlog look.

Apply the following review feedback during finalization:
[Insert your review feedback here]

Important:
Do not change composition or add artificial elements.
Do not make the footage feel over-graded, overly commercial, or dramatically cinematic.
The final result should feel like beautiful real family memories finished by a professional editor.

Lock the approved preview style and refine it for full-length consistency, natural skin tones, balanced exposure, and premium YouTube delivery quality.""",
        "review_points": [
            "가족 얼굴과 피부톤이 자연스러운가",
            "여행지 색감이 예쁘지만 과하지 않은가",
            "밝기가 안정적이고 유튜브에서 보기 편한가",
            "장면 전환마다 톤이 튀지 않는가",
            "감성은 살아 있지만 과한 AI 보정 느낌은 없는가",
        ],
        "filter": "eq=contrast=1.04:brightness=0.02:saturation=1.10:gamma=0.97,colorbalance=rs=0.04:gs=0.01:bs=-0.03:rm=0.03:gm=0.01:bm=-0.02,vignette=PI/7",
    },
    "bright_resort": {
        "label": "Bright Resort Travel",
        "category": "preview+final",
        "preview_prompt": """Act as a senior video editor and colorist creating a preview version of a YouTube family travel vlog with a bright and refreshing resort-travel style.

Create a preview-grade version of this footage with a clean, airy, sunny, vibrant travel aesthetic. The video should feel joyful, fresh, open, energetic, and premium while staying realistic and family-friendly.

Preview goals:
- Make the footage bright, crisp, and visually refreshing.
- Enhance blue skies, ocean tones, pool water, greenery, and sunlit environments beautifully but naturally.
- Keep skin tones warm and healthy without becoming orange or oversaturated.
- Use clean contrast, lively color, and soft cinematic polish.
- Correct white balance and exposure inconsistencies across travel shots.
- Gently reduce haze, noise, and compression artifacts.
- Improve detail and clarity without harsh digital sharpening.
- Keep the mood upbeat, vacation-like, and easy to watch on YouTube.
- Preserve natural motion and authentic candid moments.

Important:
This is a preview pass only.
Avoid fake HDR, excessive saturation, clipped highlights, crushed shadows, or artificial tropical exaggeration.
Do not let the footage look synthetic or over-edited.

Creative direction:
bright resort vlog, refreshing family vacation, sunny and airy, clean premium YouTube travel look, vivid but realistic color, polished holiday memories

Generate a preview using representative shots that include faces, outdoor scenery, indoor scenes, and moving travel moments so the look can be reviewed before final mastering.""",
        "final_prompt": """Act as a senior finishing colorist and post-production specialist completing the final master of a bright, refreshing YouTube family travel vlog.

Use the approved bright resort-style preview as the base look and deliver a fully polished final version with clean consistency, natural energy, and premium travel-vlog quality.

Final goals:
- Preserve the approved bright and refreshing vacation mood while improving overall consistency and finish.
- Keep skin tones natural, healthy, and flattering in strong daylight and mixed lighting.
- Refine skies, ocean, greenery, sunlit streets, and resort environments so they look attractive but realistic.
- Balance exposure to maintain bright highlights without clipping and shadows without muddiness.
- Improve clarity, detail, and color separation while avoiding overprocessing.
- Smooth out shot-to-shot differences between sunny, cloudy, indoor, and evening scenes.
- Keep the image open, clean, cheerful, and polished for YouTube viewing.

Apply the following review feedback during finalization:
[Insert your review feedback here]

Important:
Do not push colors too far.
Do not create an artificial travel-ad look unless explicitly requested.
The final result should feel like a professionally finished, bright, joyful family vacation vlog.

Lock the approved preview style and refine it for full-length consistency, natural skin tones, balanced exposure, and premium YouTube delivery quality.""",
        "review_points": [
            "휴양지 색이 청량하고 밝게 보이는가",
            "하늘, 바다, 수영장 색이 과하지 않은가",
            "피부가 너무 주황색으로 치우치지 않는가",
        ],
        "filter": "eq=contrast=1.03:brightness=0.03:saturation=1.14:gamma=0.98,colorbalance=rs=0.02:gs=0.02:bs=-0.02:rh=0.01:gh=0.02:bh=-0.01,vignette=PI/8",
    },
    "refined_travel": {
        "label": "Refined Travel Film",
        "category": "preview+final",
        "preview_prompt": """Act as a senior video editor and colorist creating a preview version of a YouTube family travel vlog with a refined Japan/Europe travel-film aesthetic.

Create a preview-grade version of this footage with an elegant, tasteful, modern travel look inspired by cinematic city walks, calm lifestyle vlogs, and premium documentary-style travel content. The footage should feel cultured, polished, natural, and visually sophisticated.

Preview goals:
- Use balanced contrast, refined color separation, and a clean cinematic finish.
- Preserve realistic skin tones and authentic street atmosphere.
- Make architecture, streets, cafes, interiors, public spaces, signage, and travel details look beautiful and intentional.
- Keep colors controlled, slightly understated, and premium rather than loud or tropical.
- Improve white balance and mixed-light scenes carefully, especially in urban interiors and street footage.
- Add subtle depth and tonal richness without making the image heavy or dramatic.
- Clean up noise and digital artifacts gently.
- Maintain a calm, elegant YouTube travel aesthetic with cohesive shot matching.

Important:
This is a preview pass only.
Avoid exaggerated saturation, strong orange/teal grading, excessive warmth, or trendy overprocessed vlog effects.
Keep the result realistic, tasteful, and cinematic.

Creative direction:
refined travel film, elegant city vlog, Japan travel aesthetic, Europe travel documentary feel, premium lifestyle YouTube look, controlled color, modern cinematic realism

Generate a preview using representative shots that include faces, outdoor scenery, indoor scenes, and moving travel moments so the look can be reviewed before final mastering.""",
        "final_prompt": """Act as a senior finishing colorist and post-production specialist completing the final master of an elegant YouTube family travel vlog with a refined Japan/Europe travel-film style.

Use the approved refined travel preview as the base look and produce the final polished version with sophisticated consistency, natural realism, and premium cinematic restraint.

Final goals:
- Preserve the approved elegant travel-film mood while refining exposure, color balance, and continuity throughout the full video.
- Keep skin tones natural and flattering in mixed indoor and outdoor conditions.
- Enhance architecture, city atmosphere, food scenes, cafes, walking shots, and environmental details with subtle richness.
- Maintain controlled saturation, clean contrast, and a polished modern finish.
- Improve shot matching across daylight, cloudy streets, train or subway scenes, interiors, and night footage.
- Reduce artifacts and noise while preserving texture and realism.
- Keep the tone calm, stylish, memorable, and highly watchable on YouTube.

Apply the following review feedback during finalization:
[Insert your review feedback here]

Important:
Do not make the footage too dramatic, too warm, or too commercial.
The final result should feel like premium travel memories with a sophisticated editorial finish.

Lock the approved preview style and refine it for full-length consistency, natural skin tones, balanced exposure, and premium YouTube delivery quality.""",
        "review_points": [
            "색감이 차분하고 세련되게 느껴지는가",
            "과한 휴양지 광고 톤으로 가지 않는가",
            "도시/실내/걷는 장면이 정돈돼 보이는가",
        ],
        "filter": "eq=contrast=1.02:brightness=0.01:saturation=0.96:gamma=0.98,colorbalance=rs=0.01:gs=0.00:bs=-0.02:rm=0.01:gm=0.00:bm=-0.01,vignette=PI/8",
    },
    "shortform_bold": {
        "label": "Shortform Bold",
        "category": "preview+final",
        "preview_prompt": """Act as a senior video editor and colorist creating a preview version of a short-form YouTube family travel highlight video optimized for Shorts/Reels style viewing.

Create a preview-grade version of this footage with a bold, clean, eye-catching, high-retention travel-vlog look designed for short-form platforms. The result should feel energetic, polished, bright, and instantly engaging while still preserving family warmth and realism.

Preview goals:
- Increase visual impact quickly with strong clarity, clean contrast, and attractive color.
- Keep skin tones natural and family-friendly even with a more energetic look.
- Make scenery, motion, and travel highlights pop immediately on mobile screens.
- Improve brightness, color separation, and detail for fast-scrolling viewers.
- Clean up noise, artifacts, and softness without introducing halos or fake sharpness.
- Preserve natural motion and avoid artificial processing.
- Keep the look premium and modern, not cheap or overly filtered.

Important:
This is a preview pass only.
Do not oversaturate, over-sharpen, or create an aggressive social-media filter look.
The result should be punchy and scroll-stopping, but still tasteful and authentic.

Creative direction:
short-form travel highlight, premium mobile-first vlog, bright and punchy, clean cinematic energy, polished family moments, high-retention visual style

Generate a preview using representative shots that include faces, outdoor scenery, indoor scenes, and moving travel moments so the look can be reviewed before final mastering.""",
        "final_prompt": """Act as a senior finishing colorist and post-production specialist completing the final master of a short-form YouTube family travel highlight video for Shorts/Reels style viewing.

Use the approved short-form preview look as the base and deliver a final version with strong mobile-screen impact, clean polish, and consistent high-energy visual quality.

Final goals:
- Preserve the approved bold short-form style while improving shot consistency and finishing detail.
- Keep the image bright, clean, and immediately engaging on small screens.
- Make travel highlights visually pop without sacrificing realism or skin tone quality.
- Refine contrast, color separation, sharpness, and exposure for premium short-form viewing.
- Keep motion natural and avoid artificial processing artifacts.
- Improve detail and clarity while avoiding harsh halos, fake HDR, or oversmoothing.
- Maintain a fun, upbeat, family-friendly travel vibe.

Apply the following review feedback during finalization:
[Insert your review feedback here]

Important:
Do not make the footage look like a heavy social filter.
Do not sacrifice natural family moments for excessive visual intensity.
The final result should feel energetic, premium, and authentic.

Lock the approved preview style and refine it for full-length consistency, natural skin tones, balanced exposure, and premium YouTube delivery quality.""",
        "review_points": [
            "모바일 화면에서 바로 눈에 들어오는가",
            "과장된 필터 느낌 없이 선명한가",
            "가족 얼굴이 과하게 날카롭거나 붉지 않은가",
        ],
        "filter": "eq=contrast=1.08:brightness=0.02:saturation=1.18:gamma=0.96,colorbalance=rs=0.03:gs=0.00:bs=-0.03:rm=0.02:gm=0.01:bm=-0.02,unsharp=5:5:0.5:5:5:0.0,vignette=PI/7",
    },
    "teal_orange_cinematic": {
        "label": "Teal Orange Cinematic",
        "category": "preview-only",
        "preview_prompt": "Create a cinematic preview with stronger teal-orange separation for comparison only. Keep skin tones usable and avoid fake HDR or crushed contrast.",
        "final_prompt": "Use the approved teal-orange cinematic preview as the base only if the reviewer explicitly selects this look. Keep skin tones natural and avoid commercial over-grading.",
        "review_points": [
            "피부가 너무 주황색으로 치우치지 않는가",
            "그림자가 과하게 청록으로 밀리지 않는가",
            "여행 영상 분위기와 어울리는가",
        ],
        "filter": "eq=contrast=1.08:brightness=0.01:saturation=1.12:gamma=0.95,colorbalance=rs=0.06:gs=-0.02:bs=-0.08:rm=0.04:gm=0.00:bm=-0.04:rh=0.06:gh=0.02:bh=-0.03,vignette=PI/6",
    },
}


LOOK_PRESET_ALIASES = {
    "warm_travel_film": "warm_family_vlog",
    "warm": "warm_family_vlog",
    "resort": "bright_resort",
    "refined": "refined_travel",
    "shortform": "shortform_bold",
    "teal_orange": "teal_orange_cinematic",
}


DEFAULT_PREVIEW_PRESETS = ["warm_family_vlog", "bright_resort"]


def resolve_preset_id(preset_id: str) -> str:
    key = preset_id.strip()
    return LOOK_PRESET_ALIASES.get(key, key)


def get_look_preset(preset_id: str) -> Dict[str, object]:
    resolved = resolve_preset_id(preset_id)
    if resolved not in LOOK_PRESETS:
        raise KeyError(f"Unknown look preset: {preset_id}")
    return {"id": resolved, **LOOK_PRESETS[resolved]}


def collect_look_presets(preset_ids: List[str]) -> List[Dict[str, object]]:
    seen = set()
    presets = []
    for preset_id in preset_ids:
        preset = get_look_preset(preset_id)
        if preset["id"] in seen:
            continue
        seen.add(preset["id"])
        presets.append(preset)
    return presets
