#!/usr/bin/env python3
"""Convert a Facebook "Download Your Information" JSON export into Markdown.

Usage:
    python3 scripts/fb_to_md.py            # unzip ./*.zip into raw/, then convert
    python3 scripts/fb_to_md.py --raw DIR  # convert an already-extracted export

Output (repo root):
    posts/YYYY/YYYY-MM-DD-HHMM-<slug>.md   one file per post, YAML front matter
    albums/<album>.md                      album pages
    media/YYYY/<file>                      images (videos skipped)
    index.jsonl                            one JSON line per post (for LLM/search)
    TIMELINE.md                            generated table of contents by year
"""

import argparse
import datetime as dt
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp"}
TZ = dt.timezone(dt.timedelta(hours=9))  # KST; change if needed


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
        return fix(obj)
    if isinstance(obj, list):
        return [fix_all(x) for x in obj]
    if isinstance(obj, dict):
        return {fix(k): fix_all(v) for k, v in obj.items()}
    return obj


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return fix_all(json.load(f))


def slugify(text, n=40):
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"[^\w\s가-힣-]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    return text[:n].strip("-").lower() or "post"


def yaml_str(s):
    return json.dumps(s, ensure_ascii=False)  # JSON strings are valid YAML


def to_dt(ts):
    return dt.datetime.fromtimestamp(ts, TZ)


# ---------- extraction ----------

def unzip_all(raw):
    zips = sorted(ROOT.glob("*.zip"))
    if not zips:
        return
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
    """Directory whose children include 'your_facebook_activity' or 'posts'."""
    for name in ("your_facebook_activity", "posts"):
        hits = sorted(raw.rglob(name), key=lambda p: len(p.parts))
        for h in hits:
            if h.is_dir():
                return h.parent
    sys.exit(f"Facebook export not found under {raw}")


class MediaResolver:
    def __init__(self, root):
        self.root = root
        self.by_name = None

    def find(self, uri):
        p = self.root / uri
        if p.exists():
            return p
        if self.by_name is None:  # lazy basename index for odd layouts
            self.by_name = {f.name: f for f in self.root.rglob("*") if f.is_file()}
        return self.by_name.get(Path(uri).name)


def post_files(root):
    pats = ["**/posts/your_posts*.json", "**/posts/your_posts__check_ins__photos_and_videos*.json"]
    seen = set()
    for pat in pats:
        for f in sorted(root.glob(pat)):
            if f not in seen:
                seen.add(f)
                yield f


def parse_post(p):
    text_parts, updated = [], None
    for d in p.get("data", []):
        if d.get("post"):
            text_parts.append(d["post"])
        if d.get("update_timestamp"):
            updated = d["update_timestamp"]

    media, links, places, extra_text = [], [], [], []
    for att in p.get("attachments", []):
        for d in att.get("data", []):
            if "media" in d:
                m = d["media"]
                media.append({
                    "uri": m.get("uri", ""),
                    "ts": m.get("creation_timestamp"),
                    "description": m.get("description") or m.get("title") or "",
                })
            if "external_context" in d:
                url = d["external_context"].get("url")
                if url:
                    links.append(url)
            if "place" in d:
                places.append(d["place"].get("name", ""))
            if "text" in d and d["text"]:
                extra_text.append(d["text"])

    return {
        "timestamp": p.get("timestamp") or (media[0]["ts"] if media else None),
        "updated": updated,
        "title": p.get("title", ""),
        "text": "\n\n".join(text_parts + extra_text).strip(),
        "media": media,
        "links": links,
        "places": [x for x in places if x],
        "tags": [t.get("name", t) if isinstance(t, dict) else t for t in p.get("tags", [])],
    }


# ---------- output ----------

def copy_media(item, year, resolver, stats):
    src = resolver.find(item["uri"])
    if src is None:
        stats["missing_media"] += 1
        return None
    if src.suffix.lower() not in IMAGE_EXT:
        stats["skipped_video"] += 1
        return None
    rel = Path("media") / str(year) / src.name
    dst = ROOT / rel
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        stats["images"] += 1
    return rel.as_posix()


def write_post(post, resolver, stats, used_names):
    d = to_dt(post["timestamp"])
    images = []
    for m in post["media"]:
        rel = copy_media(m, d.year, resolver, stats)
        if rel:
            images.append({"path": rel, "description": m["description"]})

    if not post["text"] and not images and not post["links"] and not post["places"]:
        stats["empty"] += 1
        return None

    base = f"{d:%Y-%m-%d-%H%M}-{slugify(post['text'] or post['title'])}"
    name, i = base, 2
    while name in used_names:
        name, i = f"{base}-{i}", i + 1
    used_names.add(name)
    rel = Path("posts") / str(d.year) / f"{name}.md"

    fm = ["---", f"date: {d.isoformat()}"]
    if post["updated"]:
        fm.append(f"updated: {to_dt(post['updated']).isoformat()}")
    if post["title"]:
        fm.append(f"fb_title: {yaml_str(post['title'])}")
    for key in ("places", "tags", "links"):
        if post[key]:
            fm.append(f"{key}: [{', '.join(yaml_str(x) for x in post[key])}]")
    if images:
        fm.append(f"images: [{', '.join(yaml_str(x['path']) for x in images)}]")
    fm.append("---")

    body = [post["text"]] if post["text"] else []
    for img in images:
        alt = img["description"].replace("\n", " ")[:100]
        body.append(f"![{alt}](../../{img['path']})")
        if img["description"] and img["description"] != post["text"]:
            body.append(f"> {img['description']}")
    for url in post["links"]:
        if url not in post["text"]:
            body.append(f"<{url}>")

    out = ROOT / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(fm) + "\n\n" + "\n\n".join(body) + "\n", encoding="utf-8")
    stats["posts"] += 1

    return {
        "date": d.isoformat(),
        "file": rel.as_posix(),
        "text": post["text"],
        "images": [x["path"] for x in images],
        "links": post["links"],
        "places": post["places"],
        "tags": post["tags"],
    }


def write_albums(root, resolver, stats):
    rows = []
    for f in sorted(root.glob("**/posts/album/*.json")):
        album = load_json(f)
        photos = album.get("photos", [])
        name = album.get("name") or f.stem
        lines = [f"# {name}", ""]
        if album.get("description"):
            lines += [album["description"], ""]
        for ph in photos:
            ts = ph.get("creation_timestamp") or album.get("last_modified_timestamp") or 0
            rel = copy_media({"uri": ph.get("uri", "")}, to_dt(ts).year, resolver, stats)
            if not rel:
                continue
            desc = (ph.get("description") or ph.get("title") or "").strip()
            lines.append(f"![{desc[:100]}](../{rel})")
            if desc:
                lines.append(f"> {desc}")
            lines.append("")
        out = ROOT / "albums" / f"{slugify(name, 60)}-{f.stem}.md"
        out.parent.mkdir(exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
        rows.append((name, out.relative_to(ROOT).as_posix(), len(photos)))
        stats["albums"] += 1
    return rows


def write_timeline(entries, albums):
    by_year = {}
    for e in entries:
        by_year.setdefault(e["date"][:4], []).append(e)
    lines = ["# Facebook Timeline", "",
             f"{len(entries)} posts · {sum(len(e['images']) for e in entries)} images · "
             f"{min(by_year, default='-')}–{max(by_year, default='-')}", ""]
    lines += [" · ".join(f"[{y}](#{y}) ({len(by_year[y])})" for y in sorted(by_year, reverse=True)), ""]
    for y in sorted(by_year, reverse=True):
        lines += [f"## {y}", ""]
        for e in by_year[y]:
            preview = re.sub(r"\s+", " ", e["text"])[:80] or "(photo)"
            pic = " 📷" if e["images"] else ""
            lines.append(f"- {e['date'][:10]} [{preview}]({e['file']}){pic}")
        lines.append("")
    if albums:
        lines += ["## Albums", ""]
        lines += [f"- [{n}]({p}) ({c})" for n, p, c in albums]
    (ROOT / "TIMELINE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, default=ROOT / "raw")
    ap.add_argument("--clean", action="store_true", help="delete posts/ albums/ media/ first")
    args = ap.parse_args()

    if args.raw == ROOT / "raw":
        unzip_all(args.raw)
    export_root = find_export_root(args.raw)
    print(f"export root: {export_root}")

    if args.clean:
        for d in ("posts", "albums", "media"):
            shutil.rmtree(ROOT / d, ignore_errors=True)

    resolver = MediaResolver(export_root)
    stats = dict(posts=0, images=0, albums=0, empty=0, dupes=0, skipped_video=0, missing_media=0)

    raw_posts, seen = [], set()
    for f in post_files(export_root):
        data = load_json(f)
        if isinstance(data, dict):  # older exports wrap the list
            data = next((v for v in data.values() if isinstance(v, list)), [])
        for p in data:
            post = parse_post(p)
            if not post["timestamp"]:
                continue
            key = (post["timestamp"], post["text"], tuple(m["uri"] for m in post["media"]))
            if key in seen:
                stats["dupes"] += 1
                continue
            seen.add(key)
            raw_posts.append(post)

    raw_posts.sort(key=lambda p: p["timestamp"])
    used, entries = set(), []
    for post in raw_posts:
        e = write_post(post, resolver, stats, used)
        if e:
            entries.append(e)

    albums = write_albums(export_root, resolver, stats)

    with open(ROOT / "index.jsonl", "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    write_timeline(entries, albums)

    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
