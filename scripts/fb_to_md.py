#!/usr/bin/env python3
"""Convert a Facebook "Download Your Information" JSON export into monthly Markdown.

Usage:
    python3 scripts/fb_to_md.py --raw ~/Downloads   # dir containing your_facebook_activity/
    python3 scripts/fb_to_md.py                     # unzip ./*.zip into raw/, then convert

Output (repo root):
    YYYY/YYYY-MM.md      one diary-style file per month (posts + photos, chronological)
    YYYY/README.md       month table for the year (room for a written summary)
    YYYY/media/          original images (videos skipped)
    index.jsonl          one JSON line per entry, with file#anchor (for LLM/search)
    README.md            timeline section regenerated between markers
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp"}
TZ = dt.timezone(dt.timedelta(hours=9))  # KST
MENTION = re.compile(r"@\[\d+:\d+:([^\]]+)\]")  # @[id:2048:Name] -> Name
WEEKDAY = "월화수목금토일"
WIDTH_ONE, WIDTH_MANY, WIDTH_THUMB = 480, 300, 200


# ---------- helpers ----------

def fix(s):
    """Facebook exports UTF-8 bytes escaped as latin-1 code points (mojibake)."""
    if not isinstance(s, str):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def fix_all(obj):
    if isinstance(obj, str):
        return MENTION.sub(r"\1", fix(obj))
    if isinstance(obj, list):
        return [fix_all(x) for x in obj]
    if isinstance(obj, dict):
        return {fix(k): fix_all(v) for k, v in obj.items()}
    return obj


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return fix_all(json.load(f))


def to_dt(ts):
    return dt.datetime.fromtimestamp(ts, TZ)


def headline(text, n=40):
    line = re.sub(r"https?://\S+", "", text or "").strip().split("\n")[0].strip()
    return line[:n] + ("…" if len(line) > n else "")


# ---------- export discovery ----------

def unzip_all(raw):
    zips = sorted(ROOT.glob("*.zip"))
    raw.mkdir(exist_ok=True)
    for z in zips:
        marker = raw / f".done-{z.name}"
        if marker.exists():
            continue
        print(f"unzip {z.name} ...")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(raw)
        marker.touch()


def find_export_root(raw):
    """Directory that contains your_facebook_activity/ (URIs are relative to it)."""
    raw = raw.expanduser().resolve()
    if raw.name == "your_facebook_activity":
        return raw.parent
    hits = sorted(raw.rglob("your_facebook_activity"), key=lambda p: len(p.parts))
    for h in hits:
        if h.is_dir() and (h / "posts").is_dir():
            return h.parent
    sys.exit(f"your_facebook_activity/ not found under {raw}")


class Media:
    """Resolves export URIs and copies images to YYYY/media/ once."""

    def __init__(self, root, stats):
        self.root, self.stats = root, stats
        self.by_name = None
        self.copied = {}  # uri -> repo-relative path

    def find(self, uri):
        p = self.root / uri
        if p.exists():
            return p
        if self.by_name is None:
            self.by_name = {f.name: f for f in self.root.rglob("*") if f.is_file()}
        return self.by_name.get(Path(uri).name)

    def is_image(self, uri):
        return Path(uri).suffix.lower() in IMAGE_EXT

    def copy(self, uri, ts):
        if uri in self.copied:
            return self.copied[uri]
        src = self.find(uri)
        if src is None:
            self.stats["missing_media"] += 1
            return None
        d = to_dt(ts)
        rel = Path(str(d.year)) / "media" / f"{d:%Y-%m-%d}_{src.name}"
        dst = ROOT / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        self.stats["images"] += 1
        self.copied[uri] = rel.as_posix()
        return self.copied[uri]


# ---------- parsing ----------
# Entry: {ts, kind, text, photos:[{uri, desc}], videos:int, links, places, label}

def parse_posts(posts_dir):
    entries, seen = [], set()
    for f in sorted(posts_dir.glob("your_posts*.json")):
        data = load_json(f)
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        for p in data:
            texts, photos, videos, links, places = [], [], 0, [], []
            for d in p.get("data", []):
                if d.get("post"):
                    texts.append(d["post"])
            for att in p.get("attachments", []):
                for d in att.get("data", []):
                    if "media" in d:
                        m = d["media"]
                        uri = m.get("uri", "")
                        if Path(uri).suffix.lower() in IMAGE_EXT:
                            desc = m.get("description") or ""
                            photos.append({"uri": uri, "desc": desc, "ts": m.get("creation_timestamp")})
                        else:
                            videos += 1
                            if m.get("description"):
                                texts.append(m["description"])
                    if d.get("external_context", {}).get("url"):
                        links.append(d["external_context"]["url"])
                    if d.get("place", {}).get("name"):
                        places.append(d["place"]["name"])
                    if d.get("text"):
                        texts.append(d["text"])
            ts = p.get("timestamp") or next((x["ts"] for x in photos if x["ts"]), None)
            if not ts:
                continue
            text = "\n\n".join(dict.fromkeys(t.strip() for t in texts if t.strip()))
            key = (ts, text, tuple(x["uri"] for x in photos))
            if key in seen:
                continue
            seen.add(key)
            if not (text or photos or videos or links or places):
                continue
            entries.append(dict(ts=ts, kind="post", text=text, photos=photos, videos=videos,
                                links=links, places=places, label=p.get("title", "")))
    return entries


def label_value(p, name):
    for lv in p.get("label_values", []):
        if lv.get("label") == name:
            return lv.get("value") or ""
    return ""


def parse_other_pages(posts_dir):
    f = posts_dir / "posts_on_other_pages_and_profiles.json"
    if not f.exists():
        return []
    out = []
    for p in load_json(f):
        msg = label_value(p, "Message").strip()
        if not msg:
            continue
        target = label_value(p, "Target")
        out.append(dict(ts=p["timestamp"], kind="other", text=msg, photos=[], videos=0, links=[],
                        places=[], label=f"다른 프로필에 남긴 글{' → ' + target if target else ''}"))
    return out


def parse_loose_photos(posts_dir, used_uris):
    """Photos not attached to any post: uncategorized + album photos, grouped by (source, day)."""
    groups = defaultdict(list)

    f = posts_dir / "your_uncategorized_photos.json"
    if f.exists():
        data = load_json(f)
        for ph in data.get("other_photos_v2", []) if isinstance(data, dict) else data:
            if ph.get("uri") in used_uris or not ph.get("creation_timestamp"):
                continue
            day = to_dt(ph["creation_timestamp"]).date()
            groups[("사진", day)].append(ph)

    for af in sorted(posts_dir.glob("album/*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        album = load_json(af)
        for ph in album.get("photos", []):
            ts = ph.get("creation_timestamp") or album.get("last_modified_timestamp")
            if ph.get("uri") in used_uris or not ts:
                continue
            ph = {**ph, "creation_timestamp": ts}
            groups[(f"앨범: {album.get('name', af.stem)}", to_dt(ts).date())].append(ph)

    out = []
    for (label, _), phs in groups.items():
        phs.sort(key=lambda x: x["creation_timestamp"])
        photos = [{"uri": x["uri"], "desc": x.get("description") or "", "ts": x["creation_timestamp"]}
                  for x in phs if Path(x["uri"]).suffix.lower() in IMAGE_EXT]
        if photos:
            out.append(dict(ts=phs[0]["creation_timestamp"], kind="photos", text="", photos=photos,
                            videos=0, links=[], places=[], label=label))
    return out


# ---------- rendering ----------

def render_entry(e, media, anchor):
    d = to_dt(e["ts"])
    when = f"{d.month}월 {d.day}일 ({WEEKDAY[d.weekday()]}) {d:%H:%M}"
    if e["kind"] == "photos":
        title = f"📷 {e['label']} ({len(e['photos'])}장)"
    elif e["kind"] == "other":
        title = f"↪ {headline(e['text'])}"
    else:
        title = headline(e["text"]) or ("📷 사진" if e["photos"] else e["label"] or "게시물")

    lines = [f'<a id="{anchor}"></a>', f"### {when} · {title}", ""]
    if e["kind"] == "other":
        lines += [f"*{e['label']}*", ""]
    if e["places"]:
        lines += [f"📍 {', '.join(e['places'])}", ""]
    if e["text"]:
        lines += [e["text"], ""]

    paths = []
    for ph in e["photos"]:
        rel = media.copy(ph["uri"], ph.get("ts") or e["ts"])
        if rel:
            paths.append((os.path.relpath(rel, str(d.year)), ph["desc"], rel))
    if paths:
        w = WIDTH_THUMB if e["kind"] == "photos" else WIDTH_ONE if len(paths) == 1 else WIDTH_MANY
        lines.append(" ".join(
            f'<img src="{p}" width="{w}" alt="{desc[:80].replace(chr(34), "").replace(chr(10), " ")}">'
            for p, desc, _ in paths))
        lines.append("")
        captions = [desc for _, desc, _ in paths if desc.strip() and desc.strip() not in e["text"]]
        lines += [f"> {c.replace(chr(10), ' ')}" for c in dict.fromkeys(captions)]
        if captions:
            lines.append("")
    if e["videos"]:
        lines += [f"🎬 동영상 {e['videos']}개 (생략)", ""]
    for url in e["links"]:
        if url not in e["text"]:
            lines += [f"🔗 <{url}>", ""]

    return lines, [rel for _, _, rel in paths]


def write_month(ym, entries, media, index):
    year, month = ym
    rel = Path(str(year)) / f"{year}-{month:02d}.md"
    body, n_img, used = [], 0, set()
    for e in entries:
        d = to_dt(e["ts"])
        anchor, i = f"p{d:%m%d-%H%M}", 2
        while anchor in used:
            anchor, i = f"p{d:%m%d-%H%M}-{i}", i + 1
        used.add(anchor)
        lines, imgs = render_entry(e, media, anchor)
        body += lines + [""]
        n_img += len(imgs)
        index.append({
            "date": d.isoformat(), "kind": e["kind"], "file": f"{rel.as_posix()}#{anchor}",
            "text": e["text"], "images": imgs, "videos": e["videos"],
            "links": e["links"], "places": e["places"], "label": e["label"],
        })
    n_posts = sum(1 for e in entries if e["kind"] != "photos")
    head = ["---", f"month: {year}-{month:02d}", f"entries: {len(entries)}",
            f"posts: {n_posts}", f"images: {n_img}", "---", "",
            f"# {year}년 {month}월", "", f"[← {year}년](README.md)", ""]
    (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / rel).write_text("\n".join(head + body).rstrip() + "\n", encoding="utf-8")
    first = next((headline(e["text"], 50) for e in entries if e["text"]), "")
    return dict(year=year, month=month, file=rel.name, posts=n_posts, images=n_img, first=first)


def write_year_readme(year, months):
    path = ROOT / str(year) / "README.md"
    summary = ""
    if path.exists():  # keep a hand/LLM-written summary between markers
        m = re.search(r"<!-- summary:start -->\n(.*?)<!-- summary:end -->", path.read_text(), re.S)
        summary = m.group(1) if m else ""
    lines = [f"# {year}", "", "<!-- summary:start -->", summary.rstrip() or "_(요약 예정)_",
             "<!-- summary:end -->", "", "| 월 | 글 | 사진 | 첫 글 |", "|---|---:|---:|---|"]
    for m in months:
        lines.append(f"| [{m['month']}월]({m['file']}) | {m['posts']} | {m['images']} | {m['first']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_root_readme(by_year):
    path = ROOT / "README.md"
    text = path.read_text(encoding="utf-8") if path.exists() else "# facebook-archive\n"
    total_p = sum(m["posts"] for ms in by_year.values() for m in ms)
    total_i = sum(m["images"] for ms in by_year.values() for m in ms)
    lines = ["<!-- timeline:start -->", "## Timeline", "",
             f"{min(by_year)}–{max(by_year)} · 글 {total_p:,}개 · 사진 {total_i:,}장", "",
             "| 연도 | 글 | 사진 | 월 |", "|---|---:|---:|---|"]
    for y in sorted(by_year, reverse=True):
        ms = by_year[y]
        links = " ".join(f"[{m['month']}]({y}/{m['file']})" for m in ms)
        lines.append(f"| [{y}]({y}/README.md) | {sum(m['posts'] for m in ms)} | "
                     f"{sum(m['images'] for m in ms)} | {links} |")
    lines.append("<!-- timeline:end -->")
    block = "\n".join(lines)
    if "<!-- timeline:start -->" in text:
        text = re.sub(r"<!-- timeline:start -->.*<!-- timeline:end -->", lambda _: block, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, default=ROOT / "raw")
    ap.add_argument("--clean", action="store_true", help="delete generated YYYY/ dirs first (keeps summaries)")
    args = ap.parse_args()

    if args.raw == ROOT / "raw":
        unzip_all(args.raw)
    root = find_export_root(args.raw)
    posts_dir = root / "your_facebook_activity" / "posts"
    print(f"export: {posts_dir}")

    if args.clean:
        for d in ROOT.glob("[12][0-9][0-9][0-9]"):
            for f in d.iterdir():
                if f.name != "README.md":
                    shutil.rmtree(f) if f.is_dir() else f.unlink()

    stats = defaultdict(int)
    media = Media(root, stats)

    posts = parse_posts(posts_dir)
    used = {ph["uri"] for e in posts for ph in e["photos"]}
    entries = posts + parse_other_pages(posts_dir) + parse_loose_photos(posts_dir, used)
    entries.sort(key=lambda e: e["ts"])
    for e in entries:
        stats[e["kind"]] += 1

    by_month = defaultdict(list)
    for e in entries:
        d = to_dt(e["ts"])
        by_month[(d.year, d.month)].append(e)

    index, by_year = [], defaultdict(list)
    for ym in sorted(by_month):
        by_year[ym[0]].append(write_month(ym, by_month[ym], media, index))
    for y, ms in by_year.items():
        write_year_readme(y, ms)
    write_root_readme(by_year)

    with open(ROOT / "index.jsonl", "w", encoding="utf-8") as f:
        for row in index:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    stats["months"] = len(by_month)
    print(json.dumps(dict(stats), indent=2))


if __name__ == "__main__":
    main()
