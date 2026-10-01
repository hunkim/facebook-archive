#!/usr/bin/env python3
"""Caption Facebook archive images with Korean vision descriptions.

Resume-safe via captions.jsonl. Updates monthly MD files and optionally index.jsonl.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[1]
CAPTIONS_PATH = REPO / "captions.jsonl"
FAILURES_PATH = REPO / "caption_failures.jsonl"
INDEX_PATH = REPO / "index.jsonl"

IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
SRC_RE = re.compile(r"""src=["']([^"']+)["']""", re.IGNORECASE)
CAPTION_AFTER_RE = re.compile(
    r"(<img\b[^>]*>)([ \t]*\n?[ \t]*)(\*\*이미지 캡션:\*\*[^\n]*)",
    re.IGNORECASE,
)

PROMPT = (
    "이 사진을 한국어로 자세히 설명해 주세요. 검색/RAG에 유용하도록 "
    "사람(성별·대략 나이대·외모·복장·표정·행동), 장소/배경, 사물, 보이는 텍스트, "
    "분위기/상황을 구체적으로 쓰세요. 확실하지 않은 이름은 지어내지 말고 "
    '"성인 남성" 등으로 서술하세요. 캡션 본문만 한 문단으로 출력하세요 '
    "(따옴표·머리말·마크다운 없이)."
)

_lock = threading.Lock()
_cost = 0.0
_done = 0
_fail = 0


def load_env() -> None:
    env_path = Path("/home/box/.env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def load_captions() -> dict[str, str]:
    out: dict[str, str] = {}
    if not CAPTIONS_PATH.exists():
        return out
    with CAPTIONS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = o.get("path")
            cap = o.get("caption")
            if path and cap:
                out[path] = cap
    return out


def append_jsonl(path: Path, obj: dict) -> None:
    with _lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext, "image/jpeg")


def prepare_image_b64(path: Path, max_side: int = 1280, max_bytes: int = 900_000) -> tuple[str, str]:
    """Return (mime, base64) with optional downscale/recompress."""
    raw = path.read_bytes()
    mime = mime_for(path)
    if len(raw) <= max_bytes:
        try:
            with Image.open(io.BytesIO(raw)) as im:
                w, h = im.size
                if max(w, h) <= max_side:
                    return mime, base64.b64encode(raw).decode("ascii")
        except Exception:
            return mime, base64.b64encode(raw).decode("ascii")

    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            im = im.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        quality = 85
        while True:
            buf.seek(0)
            buf.truncate(0)
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes or quality <= 50:
                break
            quality -= 10
        return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")


def call_vision(path: Path, model: str, max_retries: int = 6) -> str:
    mime, b64 = prepare_image_b64(path)
    key = os.environ.get("OPENROUTER_API_KEY") or ""
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": 600,
        "temperature": 0.2,
    }
    data = json.dumps(body).encode()
    last_err: Exception | None = None
    for attempt in range(max_retries):
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/hunkim/facebook-archive",
                "X-Title": "facebook-archive-captions",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read().decode())
            global _cost
            usage = resp.get("usage") or {}
            with _lock:
                _cost += float(usage.get("cost") or 0)
            content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            content = content.strip().strip('"').strip("'")
            # strip accidental markdown header
            if content.startswith("**이미지 캡션:**"):
                content = content[len("**이미지 캡션:**") :].strip()
            if not content:
                raise RuntimeError("empty caption")
            # collapse newlines to space for single-line MD caption
            content = re.sub(r"\s*\n\s*", " ", content).strip()
            return content
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")[:500]
            last_err = RuntimeError(f"HTTP {e.code}: {err_body}")
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(min(60, 2 ** attempt) + attempt)
                continue
            raise last_err
        except Exception as e:
            last_err = e
            time.sleep(min(30, 1.5 ** attempt))
    raise RuntimeError(f"failed after retries: {last_err}")


def collect_jobs(years: list[str] | None = None) -> list[dict]:
    """Collect image jobs from MD files chronologically."""
    jobs: list[dict] = []
    year_dirs = sorted(
        [p for p in REPO.iterdir() if p.is_dir() and p.name.isdigit()],
        key=lambda p: p.name,
    )
    for ydir in year_dirs:
        if years and ydir.name not in years:
            continue
        for md in sorted(ydir.glob("????-??.md")):
            text = md.read_text(encoding="utf-8", errors="replace")
            for m in IMG_TAG_RE.finditer(text):
                tag = m.group(0)
                sm = SRC_RE.search(tag)
                if not sm:
                    continue
                src = sm.group(1)
                # relative to year folder
                img_path = (ydir / src).resolve()
                rel = f"{ydir.name}/{src.lstrip('./')}"
                # normalize media path
                if not rel.startswith(ydir.name + "/"):
                    rel = f"{ydir.name}/{src}"
                # Check if caption already immediately after this tag in MD
                after = text[m.end() : m.end() + 80]
                has_md_cap = bool(re.match(r"\s*\*\*이미지 캡션:\*\*", after))
                jobs.append(
                    {
                        "md": str(md.relative_to(REPO)),
                        "tag": tag,
                        "src": src,
                        "path": rel.replace("\\", "/"),
                        "abs": str(img_path),
                        "has_md_cap": has_md_cap,
                        "start": m.start(),
                        "end": m.end(),
                    }
                )
    return jobs


def apply_captions_to_md(captions: dict[str, str], years: list[str] | None = None) -> int:
    """Insert **이미지 캡션:** after each img lacking one. Returns files changed."""
    changed = 0
    year_dirs = sorted(
        [p for p in REPO.iterdir() if p.is_dir() and p.name.isdigit()],
        key=lambda p: p.name,
    )
    for ydir in year_dirs:
        if years and ydir.name not in years:
            continue
        for md in sorted(ydir.glob("????-??.md")):
            text = md.read_text(encoding="utf-8", errors="replace")
            out: list[str] = []
            pos = 0
            file_changed = False
            for m in IMG_TAG_RE.finditer(text):
                out.append(text[pos : m.end()])
                after = text[m.end() : m.end() + 120]
                if re.match(r"\s*\*\*이미지 캡션:\*\*", after):
                    pos = m.end()
                    continue
                sm = SRC_RE.search(m.group(0))
                if not sm:
                    pos = m.end()
                    continue
                src = sm.group(1)
                rel = f"{ydir.name}/{src.lstrip('./')}".replace("\\", "/")
                cap = captions.get(rel)
                if not cap:
                    pos = m.end()
                    continue
                # ensure newline before caption
                out.append(f"\n**이미지 캡션:** {cap}\n")
                file_changed = True
                pos = m.end()
                # skip a single trailing newline that we'd duplicate
                if pos < len(text) and text[pos] == "\n":
                    pos += 1
            out.append(text[pos:])
            if file_changed:
                new_text = "".join(out)
                # tidy: avoid triple newlines
                new_text = re.sub(r"\n{3,}", "\n\n", new_text)
                md.write_text(new_text, encoding="utf-8")
                changed += 1
    return changed


def enrich_index(captions: dict[str, str]) -> None:
    if not INDEX_PATH.exists():
        return
    lines_out: list[str] = []
    changed = False
    with INDEX_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            o = json.loads(line)
            imgs = o.get("images") or []
            if imgs:
                caps = [captions[p] for p in imgs if p in captions]
                if caps:
                    # join for search; keep list form too
                    new_field = caps if len(caps) > 1 else caps[0]
                    if o.get("image_captions") != new_field:
                        o["image_captions"] = new_field
                        changed = True
                    # also append into text for RAG if empty-ish
                    joined = " ".join(caps)
                    existing = o.get("text") or ""
                    marker = "[이미지 캡션]"
                    if marker not in existing and joined:
                        sep = "\n" if existing else ""
                        o["text"] = f"{existing}{sep}{marker} {joined}"
                        changed = True
            lines_out.append(json.dumps(o, ensure_ascii=False))
    if changed:
        INDEX_PATH.write_text("\n".join(lines_out) + "\n", encoding="utf-8")


def process_one(job: dict, model: str, captions: dict[str, str]) -> tuple[str, str | None, str | None]:
    """Returns (path, caption_or_None, error_or_None)."""
    path = job["path"]
    if path in captions:
        return path, captions[path], None
    abs_path = Path(job["abs"])
    if not abs_path.exists():
        return path, None, "missing_file"
    try:
        cap = call_vision(abs_path, model=model)
        append_jsonl(CAPTIONS_PATH, {"path": path, "caption": cap})
        return path, cap, None
    except Exception as e:
        append_jsonl(FAILURES_PATH, {"path": path, "error": str(e)[:500]})
        return path, None, str(e)[:300]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", nargs="*", help="Limit to years e.g. 2007 2009")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--model", default="google/gemini-2.5-flash-lite")
    parser.add_argument("--limit", type=int, default=0, help="Max new captions this run")
    parser.add_argument("--apply-only", action="store_true", help="Only apply captions.jsonl to MD/index")
    parser.add_argument("--skip-index", action="store_true")
    parser.add_argument("--commit-every", type=int, default=0, help="Unused here; orchestrator commits")
    args = parser.parse_args()

    load_env()
    os.chdir(REPO)

    captions = load_captions()
    print(f"loaded_captions={len(captions)}", flush=True)

    if args.apply_only:
        n = apply_captions_to_md(captions, args.years)
        if not args.skip_index:
            enrich_index(captions)
        print(f"applied_md_files={n}", flush=True)
        return 0

    jobs = collect_jobs(args.years)
    todo = []
    for j in jobs:
        if j["path"] in captions:
            continue
        todo.append(j)
    # dedupe by path preserving order
    seen = set()
    uniq = []
    for j in todo:
        if j["path"] in seen:
            continue
        seen.add(j["path"])
        uniq.append(j)
    todo = uniq
    if args.limit:
        todo = todo[: args.limit]

    print(
        f"jobs_total_refs={len(jobs)} todo_unique={len(todo)} workers={args.workers} model={args.model}",
        flush=True,
    )

    global _done, _fail
    t0 = time.time()

    def run(j):
        return process_one(j, args.model, captions)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run, j): j for j in todo}
        for fut in as_completed(futs):
            path, cap, err = fut.result()
            with _lock:
                if cap and not err:
                    captions[path] = cap
                    _done += 1
                else:
                    _fail += 1
                n = _done + _fail
                if n % 25 == 0 or n == len(todo):
                    elapsed = time.time() - t0
                    rate = _done / elapsed if elapsed else 0
                    print(
                        f"progress done={_done} fail={_fail} "
                        f"cost≈${_cost:.4f} rate={rate:.2f}/s last={path}",
                        flush=True,
                    )

    # apply to MD for years we touched
    years = args.years or sorted({j["path"].split("/")[0] for j in jobs})
    n = apply_captions_to_md(captions, years if args.years else None)
    if not args.skip_index:
        enrich_index(captions)
    print(
        f"finished done={_done} fail={_fail} captions_total={len(captions)} "
        f"md_files_updated={n} cost≈${_cost:.4f} elapsed={time.time()-t0:.1f}s",
        flush=True,
    )
    return 0 if _fail == 0 or _done > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
