import pytest

from gemini_drawer.core.prompt_references import minimum_reference_images


@pytest.mark.parametrize("prompt, expected", [
    ("图一、图二、图三角色簇拥在一起，共同用手臂和身体比心。", 3),
    ("使用图2/3/4/5各角色的服装。", 5),
    ("第二到六张图是五个标准的玩偶，用作参考。", 6),
    ("参考第1~3图片，并将第4张图片中的角色转换为同一画风。", 4),
    ("我上传了四幅参考图，根据上传顺序安排角色。", 4),
    ("以两张图作为角色参考。", 2),
    ("融合这两张照片，保留人物原本的表情。", 2),
    ("使用多张参考图来还原服装。", 2),
    ("从参考图２保留主体，在图１的背景里合影。", 2),
    ("Replace the character in {{Image 2}} with the one from image_1.", 2),
    ("Use the two reference images as character designs.", 2),
    ("Transfer the palette of the third reference image.", 3),
    ("保留原图角色的长相，画面包含两个人物和三幅挂画。", 1),
    ("用同一张参考图做四格漫画，输出4张图片。", 1),
    ("整体保持参考图二次元风格，竖图 9:16，构图16：9。", 1),
    ("输出角色三视图，并保持参考图的角色设计一致。", 1),
    ("Read the first reference image and render two images.", 1),
])
def test_infers_minimum_from_reference_images(prompt, expected):
    assert minimum_reference_images(prompt) == expected
