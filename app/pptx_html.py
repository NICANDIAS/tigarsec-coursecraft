"""
Reads a lesson deck's .pptx file and describes every slide as plain data - background,
and each shape's exact position, size, colours, text and fonts - so the browser can draw
the SAME slides the lecturer downloads (present mode and live lessons), instead of a
separate look-alike design.

Positions are in CSS pixels at 96 per inch (the slide is 1280 x 720), font sizes in points,
colours as hex. Only what CourseCraft's own PowerPoint generator produces needs to be
understood: text boxes, filled shapes (rounded rectangle, oval, rectangle) and pictures.
Anything else on a slide (e.g. added by hand in PowerPoint) is drawn as its outline box
with its text, rather than silently dropped.
"""
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, MSO_AUTO_SHAPE_TYPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR

EMU_PER_PX = 9525          # 914400 EMU per inch / 96 px per inch
THEME_FONT = "Calibri"     # the default theme font python-pptx's blank template uses
DEFAULT_SIZE_PT = 18.0     # PowerPoint's size for text with no size of its own
DEFAULT_TEXT = "000000"

_ALIGN = {PP_ALIGN.CENTER: "center", PP_ALIGN.RIGHT: "right", PP_ALIGN.JUSTIFY: "justify"}
_ANCHOR = {MSO_ANCHOR.MIDDLE: "middle", MSO_ANCHOR.BOTTOM: "bottom"}


def _px(emu) -> float:
    return round((emu or 0) / EMU_PER_PX, 2)


def _hex(color_format):
    try:
        if color_format is not None and color_format.type is not None and color_format.rgb is not None:
            return str(color_format.rgb)
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def _fill(fill):
    try:
        if fill.type == 1:  # solid
            return str(fill.fore_color.rgb)
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def _text(shape, default_anchor="top"):
    tf = shape.text_frame
    paragraphs = []
    for p in tf.paragraphs:
        runs = []
        for r in p.runs:
            f = r.font
            runs.append({"text": r.text, "size": f.size.pt if f.size else DEFAULT_SIZE_PT, "bold": bool(f.bold),
                         "italic": bool(f.italic), "color": _hex(f.color) or DEFAULT_TEXT, "font": f.name or THEME_FONT})
        paragraphs.append({"align": _ALIGN.get(p.alignment, "left"),
                           "space_after": p.space_after.pt if p.space_after is not None else 0, "runs": runs})
    anchor = _ANCHOR.get(tf.vertical_anchor, default_anchor if tf.vertical_anchor is None else "top")
    return {"paragraphs": paragraphs, "wrap": tf.word_wrap is not False,
            "insets": [_px(tf.margin_left), _px(tf.margin_top), _px(tf.margin_right), _px(tf.margin_bottom)], "anchor": anchor}


def read_slides(path: str) -> dict:
    """{"width": px, "height": px, "slides": [{"background": hex, "elements": [...]}]}"""
    prs = Presentation(path)
    slides = []
    for s_index, slide in enumerate(prs.slides):
        background = _fill(slide.background.fill) or "FFFFFF"
        elements = []
        for z, shape in enumerate(slide.shapes):
            box = {"x": _px(shape.left), "y": _px(shape.top), "w": _px(shape.width), "h": _px(shape.height)}
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                elements.append({"kind": "picture", **box, "media": f"{s_index}/{z}"})
                continue
            element = {"kind": "text", **box}
            if shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
                try:
                    geometry = shape.auto_shape_type
                except (NotImplementedError, ValueError):
                    geometry = None
                element["kind"] = "shape"
                element["fill"] = _fill(shape.fill)
                # python-pptx's default shape style points at the theme's 3rd effect (a soft
                # outer shadow) - PowerPoint draws it, so the browser does too.
                effect = shape._element.xpath("./p:style/a:effectRef/@idx")
                element["shadow"] = bool(effect) and effect[0] not in ("0", "1") and not shape._element.xpath(".//a:effectLst")
                element["geometry"] = ("ellipse" if geometry == MSO_AUTO_SHAPE_TYPE.OVAL else
                                       "roundRect" if geometry == MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE else "rect")
                if element["geometry"] == "roundRect":
                    try:
                        adjust = shape.adjustments[0] if len(shape.adjustments) else 0.16667
                    except (IndexError, TypeError):
                        adjust = 0.16667
                    element["radius"] = round(min(box["w"], box["h"]) * adjust, 2)
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                element["text"] = _text(shape, default_anchor="middle" if element["kind"] == "shape" else "top")
            elif element["kind"] == "text":
                continue  # nothing to draw
            elements.append(element)
        slides.append({"background": background, "elements": elements})
    return {"width": _px(prs.slide_width), "height": _px(prs.slide_height), "slides": slides}


def picture_bytes(path: str, slide_index: int, shape_index: int):
    """(bytes, content_type) of one picture in the deck, or None."""
    prs = Presentation(path)
    slides = list(prs.slides)
    if not 0 <= slide_index < len(slides):
        return None
    shapes = list(slides[slide_index].shapes)
    if not 0 <= shape_index < len(shapes) or shapes[shape_index].shape_type != MSO_SHAPE_TYPE.PICTURE:
        return None
    image = shapes[shape_index].image
    return image.blob, image.content_type


def outline_slide_map(outline: dict) -> list[list[int]]:
    """Which .pptx slide(s) each outline slide became - mirrors render_lesson_pptx exactly:
    a content slide with a guiding question becomes TWO slides (question, then answer);
    an unknown slide type becomes none."""
    known = {"title", "retrieval_starter", "objectives", "content", "real_life_example", "activity",
             "practical", "retrieval_plenary", "objectives_review"}
    out, n = [], 0
    for slide in (outline or {}).get("slides", []):
        kind = slide.get("type")
        count = 0 if kind not in known else 2 if kind == "content" and slide.get("guiding_question") else 1
        out.append(list(range(n, n + count)))
        n += count
    return out
