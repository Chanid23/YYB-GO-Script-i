# -*- coding: utf-8 -*-
"""
太平洋蓝医保 · AI 任务素材自举（可独立运行，也被 tpylyb.py 自动调用）
Date: 2026-10-28

作用：把 tpylyb.py 需要的两类素材生成到指定目录，供他人拉库后直接使用。
    meals/   餐食照片：从 Wikimedia Commons 下载实拍图（自动记录来源与许可）
    reports/ 体检报告：用 PIL 现场绘制血常规/生化/尿常规三张

用法：
    python3 tpylyb_assets_bootstrap.py                # 生成到脚本同级 tpylyb_assets
    python3 tpylyb_assets_bootstrap.py /path/to/dir   # 指定目录
    python3 tpylyb_assets_bootstrap.py --force        # 覆盖已存在素材
    python3 tpylyb_assets_bootstrap.py --meals-only    # 只下餐食图（无 PIL 时也能用）

依赖：requests（必需）；Pillow（生成体检报告需要，缺失则只做餐食图）
"""

import os
import sys
import time

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) lyb-assets/1.0"}
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# 覆盖早/午/晚，便于按日期轮换
MEAL_QUERIES = [
    ("breakfast", "breakfast plate eggs toast"),
    ("breakfast", "congee youtiao chinese breakfast"),
    ("lunch", "chinese lunch rice dishes"),
    ("lunch", "bento box lunch"),
    ("dinner", "dinner plate meat vegetables"),
    ("dinner", "noodles bowl soup"),
]

FONT_CANDIDATES = [
    "https://cdn.jsdelivr.net/gh/googlefonts/noto-cjk@main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
    "https://mirrors.tuna.tsinghua.edu.cn/github-release/googlefonts/noto-cjk/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
]

HOSPITAL = "市第一人民医院"


def _noop(msg):
    pass


# ---------------------------------------------------------------- 餐食图
def _commons_search(query, limit=4):
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrnamespace": 6, "gsrsearch": "filetype:bitmap " + query,
        "gsrlimit": limit, "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1280,
    }
    r = requests.get(COMMONS_API, params=params, headers=UA, timeout=30)
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or {}
    out = []
    for p in pages.values():
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        meta = info.get("extmetadata") or {}
        out.append({
            "url": url,
            "width": info.get("width"),
            "height": info.get("height"),
            "title": p.get("title", ""),
            "license": (meta.get("LicenseShortName") or {}).get("value", ""),
            "artist": (meta.get("Artist") or {}).get("value", ""),
        })
    return out


def fetch_meals(target, want=6, force=False, log=_noop):
    if requests is None:
        log("⚠️ 缺少 requests，无法下载餐食图：pip install requests")
        return []
    folder = os.path.join(target, "meals")
    os.makedirs(folder, exist_ok=True)
    existing = [n for n in os.listdir(folder) if n.lower().endswith((".jpg", ".jpeg", ".png"))]
    if existing and not force and len(existing) >= want:
        log("餐食图已存在 %d 张，跳过下载" % len(existing))
        return []

    index, n = [], 0
    for slot, query in MEAL_QUERIES:
        if n >= want:
            break
        try:
            cands = _commons_search(query)
        except Exception as exc:
            log("  搜索失败 %s: %s" % (query, exc))
            continue
        for cand in cands:
            if n >= want:
                break
            w = cand.get("width") or 0
            if w and w < 500:
                continue
            n += 1
            name = "meal_%02d_%s.jpg" % (n, slot)
            path = os.path.join(folder, name)
            try:
                r = requests.get(cand["url"], headers=UA, timeout=40)
                if r.status_code != 200 or len(r.content) < 20000:
                    n -= 1
                    continue
                with open(path, "wb") as f:
                    f.write(r.content)
                index.append(dict(cand, file=name, bytes=len(r.content), slot=slot))
                log("  ✅ %-30s %7d B  (%s)" % (name, len(r.content), slot))
            except Exception as exc:
                n -= 1
                log("  下载失败 %s: %s" % (cand["url"][:50], exc))
            time.sleep(0.3)
    if index:
        _write_sources(target, index)
    return index


def _write_sources(target, items):
    """记录图片出处与许可，便于二次分发时合规署名。"""
    path = os.path.join(target, "SOURCES.md")
    lines = ["# 素材来源与许可", "",
             "餐食照片取自 Wikimedia Commons，按各自的许可证使用（多为 CC BY-SA / CC0 / Public Domain）。",
             "若二次分发这些图片，请保留下表出处与作者信息。", "",
             "| 文件 | 主题 | 许可 | 作者 | 来源页 |",
             "| --- | --- | --- | --- | --- |"]
    for it in items:
        artist = (it.get("artist") or "").replace("|", "/").replace("\n", " ")[:60]
        title = (it.get("title") or "").replace("|", "/")
        lines.append("| %s | %s | %s | %s | https://commons.wikimedia.org/wiki/%s |"
                     % (it.get("file", ""), it.get("slot", ""),
                        it.get("license") or "-", artist or "-",
                        title.replace(" ", "_")))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------- 体检报告
def _font(size):
    from PIL import ImageFont
    return ImageFont.truetype(_FONT_PATH, size)


_FONT_PATH = ""


def _barcode(d, x, y, w, h, seed):
    import random
    rnd = random.Random(seed)
    cx = x
    while cx < x + w:
        bw = rnd.choice([2, 2, 3, 4])
        if rnd.random() > 0.35:
            d.rectangle([cx, y, cx + bw, y + h], fill=(0, 0, 0))
        cx += bw + rnd.choice([2, 3])


def _qrcode_block(d, x, y, size, seed):
    import random
    rnd = random.Random(seed)
    cell = max(size // 21, 1)
    for i in range(21):
        for j in range(21):
            if rnd.random() > 0.5:
                d.rectangle([x + i * cell, y + j * cell,
                             x + (i + 1) * cell - 1, y + (j + 1) * cell - 1], fill=(0, 0, 0))
    for (ox, oy) in ((0, 0), (14, 0), (0, 14)):
        d.rectangle([x + ox * cell, y + oy * cell,
                     x + (ox + 7) * cell, y + (oy + 7) * cell], outline=(0, 0, 0), width=2)


def _draw_report(out_path, title, en_title, meta, rows, conclusion, seed):
    from PIL import Image, ImageDraw
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), (252, 252, 250))
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 12], fill=(0, 92, 132))
    d.text((70, 46), HOSPITAL, font=_font(38), fill=(0, 70, 105))
    d.text((70, 96), "健康体检中心  HEALTH EXAMINATION CENTER", font=_font(20), fill=(110, 110, 110))
    d.text((W - 70, 52), "报告编号：TJ2026102800117", font=_font(20), fill=(60, 60, 60), anchor="ra")
    _barcode(d, W - 300, 84, 230, 34, seed)
    d.text((W - 70, 124), "2026102800117", font=_font(16), fill=(60, 60, 60), anchor="ra")

    d.text((W // 2, 190), title, font=_font(44), fill=(0, 0, 0), anchor="mm")
    d.text((W // 2, 238), en_title, font=_font(19), fill=(120, 120, 120), anchor="mm")
    d.line([(70, 272), (W - 70, 272)], fill=(0, 92, 132), width=3)

    y = 296
    for line in meta:
        x = 80
        for cell in line:
            d.text((x, y), cell, font=_font(23), fill=(35, 35, 35))
            x += 390
        y += 40

    y += 18
    heads = [("序号", 80), ("检验项目", 150), ("结果", 520), ("标志", 650), ("单位", 720), ("参考区间", 850)]
    d.rectangle([70, y, W - 70, y + 46], fill=(226, 238, 244))
    for name, x in heads:
        d.text((x + 8, y + 11), name, font=_font(23), fill=(0, 60, 90))
    y += 46
    for i, (item, val, unit, ref, flag) in enumerate(rows, 1):
        if i % 2 == 0:
            d.rectangle([70, y, W - 70, y + 44], fill=(246, 249, 251))
        d.text((88, y + 10), str(i), font=_font(21), fill=(60, 60, 60))
        d.text((158, y + 10), item, font=_font(22), fill=(20, 20, 20))
        color = (190, 30, 30) if flag else (20, 20, 20)
        d.text((528, y + 10), val, font=_font(22), fill=color)
        if flag:
            d.text((658, y + 10), "↑" if flag == "H" else "↓", font=_font(22), fill=color)
        d.text((728, y + 10), unit, font=_font(21), fill=(80, 80, 80))
        d.text((858, y + 10), ref, font=_font(21), fill=(80, 80, 80))
        y += 44
    d.rectangle([70, y - 44 * len(rows) - 46, W - 70, y], outline=(160, 180, 190), width=1)

    y += 26
    d.text((80, y), "检查结论 / 建议：", font=_font(26), fill=(0, 70, 105))
    y += 44
    for line in conclusion:
        d.text((100, y), line, font=_font(22), fill=(45, 45, 45))
        y += 36

    y += 40
    d.text((80, y), "检验医师：李静", font=_font(22), fill=(40, 40, 40))
    d.text((360, y), "审核医师：王强", font=_font(22), fill=(40, 40, 40))
    d.text((640, y), "报告日期：2026-10-28", font=_font(22), fill=(40, 40, 40))

    cx, cy, r = W - 210, y + 40, 92
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(200, 55, 55), width=4)
    d.text((cx, cy - 26), "市第一人民医院", font=_font(19), fill=(200, 55, 55), anchor="mm")
    d.text((cx, cy + 4), "体检专用章", font=_font(19), fill=(200, 55, 55), anchor="mm")
    d.text((cx, cy + 34), "2026.10.28", font=_font(15), fill=(200, 55, 55), anchor="mm")

    _qrcode_block(d, 90, H - 190, 126, seed + 7)
    d.text((236, H - 176), "扫码查看电子报告", font=_font(19), fill=(90, 90, 90))
    d.text((236, H - 146), "本报告仅对本次检查项目负责", font=_font(17), fill=(130, 130, 130))
    d.text((236, H - 120), "如有疑问请咨询体检中心 0551-8888xxxx", font=_font(17), fill=(130, 130, 130))

    img.save(out_path, "PNG")


def _ensure_font(target, log=_noop):
    """下载中文字体（约 16MB）；失败返回空串，报告改用英文标题。"""
    global _FONT_PATH
    path = os.path.join(target, "_font.otf")
    # 复用已有字体，避免重复下载 16MB
    for cand in (path, os.path.join(target, "sc.otf"), "/ql/data/scripts/tpylyb_assets/sc.otf"):
        if os.path.exists(cand) and os.path.getsize(cand) > 1_000_000:
            _FONT_PATH = cand
            log("  复用已有字体 %s" % cand)
            return cand
    for url in FONT_CANDIDATES:
        try:
            log("  下载中文字体 …（约 16MB，仅首次）")
            r = requests.get(url, headers=UA, timeout=180)
            if r.status_code == 200 and len(r.content) > 1_000_000:
                with open(path, "wb") as f:
                    f.write(r.content)
                _FONT_PATH = path
                log("  ✅ 字体就绪 %d B" % len(r.content))
                return path
        except Exception as exc:
            log("  字体下载失败：%s" % exc)
    log("  ⚠️ 未获取中文字体，报告将使用默认字体（中文可能显示为方框）")
    return ""


CBC = [("白细胞计数 WBC", "6.24", "10^9/L", "3.50-9.50", ""),
       ("红细胞计数 RBC", "4.86", "10^12/L", "4.30-5.80", ""),
       ("血红蛋白 HGB", "149", "g/L", "130-175", ""),
       ("红细胞压积 HCT", "44.8", "%", "40.0-50.0", ""),
       ("平均红细胞体积 MCV", "92.2", "fL", "82.0-100.0", ""),
       ("平均血红蛋白量 MCH", "30.7", "pg", "27.0-34.0", ""),
       ("血小板计数 PLT", "231", "10^9/L", "125-350", ""),
       ("中性粒细胞百分比 NEUT%", "58.6", "%", "40.0-75.0", ""),
       ("淋巴细胞百分比 LYM%", "30.8", "%", "20.0-50.0", ""),
       ("单核细胞百分比 MONO%", "7.4", "%", "3.0-10.0", ""),
       ("嗜酸性粒细胞百分比 EO%", "2.6", "%", "0.4-8.0", ""),
       ("C反应蛋白 CRP", "2.14", "mg/L", "0.00-10.00", "")]

BIO = [("谷丙转氨酶 ALT", "26", "U/L", "9-50", ""),
       ("谷草转氨酶 AST", "22", "U/L", "15-40", ""),
       ("谷草/谷丙比值 AST/ALT", "0.85", "", "0.60-1.60", ""),
       ("总胆红素 TBIL", "14.2", "umol/L", "3.4-20.5", ""),
       ("直接胆红素 DBIL", "3.6", "umol/L", "0.0-6.8", ""),
       ("总蛋白 TP", "72.4", "g/L", "65.0-85.0", ""),
       ("白蛋白 ALB", "45.6", "g/L", "40.0-55.0", ""),
       ("总胆固醇 TC", "4.28", "mmol/L", "2.80-5.20", ""),
       ("甘油三酯 TG", "1.86", "mmol/L", "0.45-1.70", "H"),
       ("高密度脂蛋白 HDL-C", "1.28", "mmol/L", "1.00-1.90", ""),
       ("低密度脂蛋白 LDL-C", "2.51", "mmol/L", "0.00-3.40", ""),
       ("空腹血糖 GLU", "5.12", "mmol/L", "3.90-6.10", ""),
       ("肌酐 CREA", "78", "umol/L", "57-97", ""),
       ("尿酸 UA", "342", "umol/L", "208-428", ""),
       ("尿素氮 BUN", "5.42", "mmol/L", "3.10-8.00", "")]

URINE = [("尿颜色 URO", "淡黄色", "", "淡黄色", ""),
         ("尿透明度", "清晰", "", "清晰", ""),
         ("尿比重 SG", "1.020", "", "1.003-1.030", ""),
         ("尿酸碱度 PH", "6.0", "", "4.5-8.0", ""),
         ("尿蛋白 PRO", "阴性", "", "阴性", ""),
         ("尿糖 GLU", "阴性", "", "阴性", ""),
         ("尿潜血 BLD", "阴性", "", "阴性", ""),
         ("尿酮体 KET", "阴性", "", "阴性", ""),
         ("尿胆原 URO", "正常", "", "正常", ""),
         ("尿白细胞 LEU", "阴性", "", "阴性", ""),
         ("尿亚硝酸盐 NIT", "阴性", "", "阴性", ""),
         ("尿沉渣红细胞", "1.2", "/HP", "0.0-3.0", "")]


def make_reports(target, force=False, log=_noop):
    folder = os.path.join(target, "reports")
    os.makedirs(folder, exist_ok=True)
    existing = [n for n in os.listdir(folder) if n.lower().endswith((".png", ".jpg", ".pdf"))]
    if existing and not force:
        log("体检报告已存在 %d 份，跳过生成" % len(existing))
        return existing
    try:
        import PIL  # noqa: F401
    except ImportError:
        log("⚠️ 缺少 Pillow，跳过体检报告生成：pip install pillow")
        return []

    if not _ensure_font(target, log):
        log("⚠️ 无中文字体，报告标题改用英文（表格中文会显示异常）")

    meta = [("姓　名：张伟", "性　别：男", "年　龄：38 岁"),
            ("体检编号：TJ2026102800117", "体检日期：2026-10-26", "报告日期：2026-10-28"),
            ("科　室：体检中心", "标本类型：静脉血", "床　号：—")]
    plan = [
        ("report_cbc.png", "健康体检报告（血常规）", "Blood Routine Examination", CBC,
         ["1. 血常规各项指标均在参考区间内，未见明显异常。",
          "2. 建议保持规律作息，均衡饮食，适量运动。"], 11),
        ("report_bio.png", "健康体检报告（生化全套）", "Biochemistry Profile", BIO,
         ["1. 甘油三酯轻度升高，建议低脂饮食、限制饮酒、增加有氧运动。",
          "2. 肝肾功能、血糖、血脂其余项目未见明显异常，建议 3-6 个月复查血脂。"], 23),
        ("report_urine.png", "健康体检报告（尿常规）", "Urine Routine Examination", URINE,
         ["1. 尿常规各项指标未见明显异常。",
          "2. 建议多饮水，注意个人卫生，每年复查一次。"], 37),
    ]
    made = []
    for name, title, en, rows, conc, seed in plan:
        path = os.path.join(folder, name)
        try:
            _draw_report(path, title, en, meta, rows, conc, seed)
            made.append(name)
            log("  ✅ %-20s %7d B" % (name, os.path.getsize(path)))
        except Exception as exc:
            log("  ❌ %s 生成失败：%s" % (name, exc))
    return made


# ---------------------------------------------------------------- 入口
def ensure_assets(target=None, want_meals=12, meals_only=False, force=False, log=_noop):
    """生成素材到 target；返回 {'meals': [...], 'reports': [...]}。"""
    target = os.path.abspath(target or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "tpylyb_assets"))
    os.makedirs(target, exist_ok=True)
    log("素材目录：%s" % target)
    meals = fetch_meals(target, want=want_meals, force=force, log=log)
    reports = [] if meals_only else make_reports(target, force=force, log=log)
    return {"meals": meals, "reports": reports}


def main(argv):
    target = None
    mode = {}
    for a in argv[1:]:
        if a == "--force":
            mode["force"] = True
        elif a == "--meals-only":
            mode["meals_only"] = True
        elif not a.startswith("-"):
            target = a
    print("太平洋蓝医保 · 素材初始化")
    res = ensure_assets(target, force=mode.get("force", False),
                        meals_only=mode.get("meals_only", False), log=print)
    print("\n完成：餐食图 %d 张（本次新增）、体检报告 %d 份"
          % (len(res["meals"]), len(res["reports"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
