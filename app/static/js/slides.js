/*
 * Draws a lesson deck slide exactly as it is in the downloaded PowerPoint, from the data
 * app/pptx_html.py reads out of the .pptx file (positions in px at 96 per inch, sizes in pt).
 * Used by present mode and by the live lesson screen, so both always match the file.
 * Text is in Calibri when the computer has it (as PowerPoint does), otherwise Carlito -
 * a free font with the SAME letter widths as Calibri, so lines wrap at the same words.
 */
(function (global) {
  var PT = 96 / 72;  // CSS px per point

  function el(tag, css) {
    var e = document.createElement(tag);
    if (css) e.style.cssText = css;
    return e;
  }

  function drawText(box, t) {
    var anchor = t.anchor === "middle" ? "center" : t.anchor === "bottom" ? "flex-end" : "flex-start";
    var frame = el("div", "position:absolute;inset:0;display:flex;flex-direction:column;justify-content:" + anchor +
      ";padding:" + t.insets[1] + "px " + t.insets[2] + "px " + t.insets[3] + "px " + t.insets[0] + "px;box-sizing:border-box;");
    t.paragraphs.forEach(function (p, i) {
      var para = el("div", "text-align:" + p.align + ";white-space:" + (t.wrap ? "pre-wrap" : "pre") +
        ";overflow-wrap:break-word;margin:0 0 " + (i < t.paragraphs.length - 1 ? p.space_after * PT : 0) + "px;");
      var biggest = 0, family = "'Calibri', Carlito, sans-serif";
      p.runs.forEach(function (r) {
        var fam = "'" + r.font + "', Carlito, sans-serif";
        var span = el("span", "font-family:" + fam + ";font-size:" + (r.size * PT) +
          "px;color:#" + r.color + ";font-weight:" + (r.bold ? 700 : 400) + ";font-style:" + (r.italic ? "italic" : "normal") + ";");
        span.textContent = r.text;
        para.appendChild(span);
        if (r.size >= biggest) { biggest = r.size; family = fam; }
      });
      // The paragraph itself uses the same font and size as its text, and the font's own
      // natural line height (Calibri / Carlito: 1.22 x size) - as PowerPoint does. Otherwise
      // the page's default font adds an invisible extra few pixels to every line.
      para.style.fontFamily = family;
      para.style.fontSize = ((biggest || 18) * PT) + "px";
      para.style.lineHeight = "normal";
      if (!p.runs.length) para.style.minHeight = "1.22em";
      frame.appendChild(para);
    });
    box.appendChild(frame);
  }

  // Draws one slide into `stage` (an element sized width x height px). mediaUrl(ref) -> picture URL.
  function drawSlide(stage, slide, mediaUrl) {
    stage.innerHTML = "";
    stage.style.background = "#" + (slide.background || "FFFFFF");
    slide.elements.forEach(function (e) {
      var box = el("div", "position:absolute;left:" + e.x + "px;top:" + e.y + "px;width:" + e.w + "px;height:" + e.h + "px;");
      if (e.kind === "picture") {
        var img = el("img", "width:100%;height:100%;display:block;");
        img.src = mediaUrl(e.media);
        img.alt = "";
        box.appendChild(img);
      } else {
        if (e.kind === "shape") {
          if (e.fill) box.style.background = "#" + e.fill;
          box.style.borderRadius = e.geometry === "ellipse" ? "50%" : e.geometry === "roundRect" ? (e.radius || 0) + "px" : "0";
          // PowerPoint's default shape style adds a soft shadow below shapes
          if (e.shadow) box.style.boxShadow = "0 2.5px 3px rgba(0,0,0,0.30)";
        }
        if (e.text) drawText(box, e.text);
      }
      stage.appendChild(box);
    });
  }

  // Scales a fixed-size stage to fit its parent, keeping the 16:9 shape (letterboxed like PowerPoint).
  function fitStage(stage, width, height) {
    var parent = stage.parentElement;
    var scale = Math.min(parent.clientWidth / width, parent.clientHeight / height);
    stage.style.width = width + "px";
    stage.style.height = height + "px";
    stage.style.transform = "scale(" + scale + ")";
    stage.style.left = ((parent.clientWidth - width * scale) / 2) + "px";
    stage.style.top = ((parent.clientHeight - height * scale) / 2) + "px";
  }

  global.CourseCraftSlides = { drawSlide: drawSlide, fitStage: fitStage };
})(window);
