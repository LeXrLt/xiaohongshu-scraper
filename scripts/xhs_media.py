"""媒体下载：图片/视频下载 + 自动下载 + 视频分析后处理。

从 xhs.py 拆分出来的媒体处理层：
- guess_ext(): URL 扩展名猜测
- download_media(): 下载笔记的图片/视频（自动使用博主/标题路径）
- auto_download_note(): 抓取笔记后自动下载图片（轻量，不打扰用户）
- post_process_note(): crawl 时的 --download / --analyze 后处理
"""

from __future__ import annotations

import random
import json
import sys
import time
from pathlib import Path

import xhs_config
import xhs_storage
from database import Connection

# curl_cffi 可用性检测（同 xhs_fetcher）
try:
    from curl_cffi.requests import get as _cget  # type: ignore
    _CURL_CFFI = True
except ImportError:
    _CURL_CFFI = False


def guess_ext(url: str, default: str = ".jpg") -> str:
    url_low = url.lower().split("?")[0]
    for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov", ".webm"):
        if url_low.endswith(ext):
            return ext
    return default


def _media_out_dir(note_id: str, conn: Connection) -> Path:
    """计算媒体输出目录: media/<博主名>/<笔记标题>/。"""
    return xhs_storage._find_media_dir(note_id, conn)


def download_media(
    note_id: str, conn: Connection, with_video: bool = True, overwrite: bool = False
) -> tuple[int, int, int, Path]:
    """下载某笔记的图片/视频。
    返回 (n_img, n_video, n_err, out_dir)。
    """
    row = xhs_storage.get_note(conn, note_id)
    if not row:
        print(f"[WARN] 笔记 {note_id} 不在 DB，跳过下载", file=sys.stderr)
        return 0, 0, 1, xhs_config.MEDIA_DIR / note_id
    raw = (row["raw_data"] or {})
    out = _media_out_dir(note_id, conn)
    out.mkdir(parents=True, exist_ok=True)

    if _CURL_CFFI:
        def _fetch_bytes(url: str) -> bytes:
            r = _cget(url, impersonate=xhs_config.IMPERSONATE_PROFILE, timeout=30)
            r.raise_for_status()
            return r.content
    else:
        import requests as _rq  # type: ignore
        def _fetch_bytes(url: str) -> bytes:
            r = _rq.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.content

    # 1) 图片
    images = xhs_storage._extract_images(raw)
    n_ok = 0
    n_err = 0
    for i, url in enumerate(images, 1):
        ext = guess_ext(url, default=".jpg")
        dst = out / f"img_{i:02d}{ext}"
        if dst.exists() and dst.stat().st_size > 0 and not overwrite:
            n_ok += 1  # 已有算成功
            continue
        try:
            data = _fetch_bytes(url)
            tmp = dst.with_suffix(dst.suffix + '.tmp')
            try:
                tmp.write_bytes(data)
                tmp.replace(dst)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            n_ok += 1
            time.sleep(random.uniform(0.3, 1.0))
        except Exception as e:
            n_err += 1
            if n_err <= 3:
                print(f"  [download] 图片下载失败 {url[:60]}...: {e}", file=sys.stderr)

    # 2) 视频（流式）
    n_video = 0
    if with_video:
        video_url = row["video_url"] or xhs_storage._extract_video_url(raw)
        if video_url:
            dst = out / "video.mp4"
            if dst.exists() and dst.stat().st_size > 0 and not overwrite:
                n_video = 1
            else:
                try:
                    tmp = dst.with_suffix('.mp4.tmp')
                    try:
                        if _CURL_CFFI:
                            r = _cget(video_url, impersonate=xhs_config.IMPERSONATE_PROFILE, timeout=60, stream=True)
                            r.raise_for_status()
                            with open(tmp, 'wb') as vf:
                                for chunk in r.iter_content(chunk_size=8192):
                                    vf.write(chunk)
                        else:
                            import requests as _rq2  # type: ignore
                            r = _rq2.get(video_url, timeout=60, stream=True,
                                         headers={"User-Agent": "Mozilla/5.0"})
                            r.raise_for_status()
                            with open(tmp, 'wb') as vf:
                                for chunk in r.iter_content(chunk_size=8192):
                                    vf.write(chunk)
                        tmp.replace(dst)
                        n_video = 1
                    except Exception:
                        tmp.unlink(missing_ok=True)
                        raise
                except Exception as e:
                    n_err += 1
                    print(f"  [download] 视频下载失败: {e}", file=sys.stderr)

    with conn:
        xhs_storage.update_media_state(conn, note_id)
    return n_ok, n_video, n_err, out


def auto_download_note(note: dict, conn: Connection) -> None:
    """抓取笔记后自动下载图片（轻量，仅图片，不打扰用户）。

    - 只下图片，不下视频（视频大，需要用户主动）
    - 静默跳过已下载的
    - 一行输出让用户知道在做什么
    """
    note_id = note.get("note_id", "")
    if not note_id:
        return
    try:
        n_img, _, n_err, out = download_media(note_id, conn, with_video=False)
        if n_img > 0 and n_err == 0:
            title = note.get("title") or note.get("description", "")[:20] or note_id
            # 计算可读的相对路径
            rel = out.relative_to(xhs_config.MEDIA_DIR)
            print(f"     图片: {rel} ({n_img}张)")
    except Exception as e:
        print(f"     [auto-download] {note_id}: {e}", file=sys.stderr)


def find_video_local(note_id: str, conn: Connection) -> Path | None:
    """查找笔记的本地视频文件路径（兼容新旧目录结构）。"""
    # 新路径: media/<博主>/<标题>/video.mp4
    out = _media_out_dir(note_id, conn)
    video = out / "video.mp4"
    if video.exists():
        return video
    # 旧路径兼容: media/<note_id>/video.mp4
    legacy = xhs_config.MEDIA_DIR / note_id / "video.mp4"
    if legacy.exists():
        return legacy
    return None


def _refresh_video_url(note_id: str, conn: Connection) -> None:
    """当 video_url 为空时，提示用户需要先抓笔记详情。

    列表 API（user_posted / search）返回摘要数据，不含完整 video URL。
    需要通过 `note <id>` 命令获取完整数据才能下载视频。
    """
    print(f"    [refresh] 笔记 {note_id} 缺少 video_url，请先运行: "
          f"python scripts/xhs.py note {note_id}", file=sys.stderr)


def post_process_note(note: dict, conn: Connection, args) -> None:
    """独立记录下载和分析进度，页面游标前移不丢失失败任务。"""
    note_id = note["note_id"]
    title = note.get("title") or note_id
    is_video = note.get("type") == "video"

    def state(kind, status, error=""):
        xhs_storage.update_crawl_state(conn, f"{kind}:{note_id}", kind, note_id, "", status, error)

    if getattr(args, "download", False):
        state("media-download", "running")
        try:
            n_img, n_vid, n_err, out = download_media(note_id, conn, with_video=True)
            done = n_err == 0 and (not is_video or n_vid > 0)
            state("media-download", "completed" if done else "failed", "" if done else "媒体未完整下载")
            print(f"    [download] 《{title}》: {n_img} 张图，{n_vid} 个视频，{n_err} 个失败", file=sys.stderr)
        except Exception as exc:
            state("media-download", "failed", str(exc))
            print(f"    [download] 《{title}》失败: {exc}", file=sys.stderr)

    if not getattr(args, "analyze", False):
        return
    kind = "video-analysis" if is_video else "image-analysis"
    state(kind, "running")
    try:
        if is_video:
            import xhs_video
            video_local = find_video_local(note_id, conn)
            if not video_local:
                if not note.get("video_url"):
                    _refresh_video_url(note_id, conn)
                download_media(note_id, conn, with_video=True)
                video_local = find_video_local(note_id, conn)
            if not video_local:
                raise RuntimeError("视频下载未完成")
            result = xhs_video.analyze_video(video_local, xhs_video.load_config())
            xhs_storage.update_video_analysis(
                conn, note_id, transcript=result.get("transcript", ""),
                ocr_text=json.dumps(result.get("ocr_results", []), ensure_ascii=False),
                summary="llm纠错" if result.get("corrected") else "",
            )
        else:
            import xhs_image
            result = xhs_image.analyze_images(note_id, conn)
            xhs_storage.update_image_analysis(conn, note_id, ocr_text=result.get("ocr_text", ""))
        done = bool(result.get("completed"))
        state(kind, "completed" if done else "failed", "" if done else "分析阶段未全部完成，等待重试")
        print(f"    [{kind}] 《{title}》: {'完成' if done else '进度已保存，等待重试'}", file=sys.stderr)
    except Exception as exc:
        state(kind, "failed", str(exc))
        print(f"    [{kind}] 《{title}》失败: {exc}", file=sys.stderr)


def resume_pending(conn: Connection, args, limit: int = 100) -> None:
    """抓取页已前移后，仍能独立重试未完成的下载和分析。"""
    kinds = []
    if getattr(args, "download", False):
        kinds.append("media-download")
    if getattr(args, "analyze", False):
        kinds.extend(["image-analysis", "video-analysis"])
    if not kinds:
        return
    rows = conn.execute("""SELECT target_id, array_agg(DISTINCT task_type) AS kinds
        FROM xhs_crawl_state WHERE task_type=ANY(%s) AND status <> 'completed'
        GROUP BY target_id ORDER BY MIN(updated_at), target_id LIMIT %s""", (kinds, limit)).fetchall()
    from types import SimpleNamespace
    for row in rows:
        note = xhs_storage.get_note(conn, row["target_id"])
        if note:
            post_process_note(dict(note), conn, SimpleNamespace(download="media-download" in row["kinds"],
                                                               analyze=any(k.endswith("analysis") for k in row["kinds"])))
