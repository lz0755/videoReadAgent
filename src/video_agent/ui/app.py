"""Local Streamlit review entry point."""

import os
from contextlib import closing
from uuid import uuid4

import streamlit as st

from video_agent.domain.models import PlaybackTimelineMap
from video_agent.ui.api_client import MAX_UPLOAD, ReviewClient, ReviewError, identifier
from video_agent.ui.playback import playback_seconds


def _intent(kind: str, values: tuple[str, ...]) -> str:
    intent = st.session_state.get(kind)
    if not intent or intent[0] != values:
        intent = (values, uuid4().hex)
        st.session_state[kind] = intent
    return str(intent[1])


def _review(client: ReviewClient) -> None:
    st.header("导入授权视频")
    upload = st.file_uploader("视频文件", max_upload_size=64)
    st.caption(
        "浏览器最多缓存 64 MiB。大文件请用 CLI：video-agent assets import，再输入返回的 asset_id。"
    )
    consent = st.checkbox("我有权在本机处理这个视频")
    if st.button("导入视频", disabled=upload is None or not consent) and upload is not None:
        if upload.size > MAX_UPLOAD:
            raise ReviewError("超过 64 MiB，请使用 CLI 导入。")
        upload.seek(0)
        st.session_state["asset_id"] = client.upload(
            upload, upload.name, upload.size, "本机页面确认处理授权"
        )
    asset = st.text_input("资产 ID", key="asset_id").strip()
    if st.session_state.get("current_asset") != asset:
        st.session_state.update(
            current_asset=asset, analysis_id="", job_id="", question_id="", offset=0
        )
    profile = st.selectbox("分析场景", ["software_tutorial", "overview"])
    mode = st.selectbox(
        "执行模式",
        ["offline", "live"],
        format_func=lambda m: "本机离线演示（Fake）" if m == "offline" else "已授权的云端模型",
    )
    if st.button("提交分析", disabled=not asset):
        path = f"/v1/assets/{identifier(asset)}/analyses"
        created = client.submit(
            path,
            {"profile": profile, "execution_mode": mode},
            _intent("analysis_intent", (asset, profile, mode)),
        )
        st.session_state.update(analysis_id=created["analysis_id"], job_id=created["job_id"])
        del st.session_state["analysis_intent"]
    analysis = st.text_input("分析 ID", key="analysis_id").strip()
    if st.session_state.get("current_analysis") != analysis:
        st.session_state.update(current_analysis=analysis, question_id="", offset=0)
    if not analysis:
        return
    identifier(analysis)
    st.button("刷新状态")
    result = client.request(
        "GET", f"/v1/analyses/{analysis}", params={"limit": 20, "offset": st.session_state.offset}
    ).json()
    if asset and result["asset_id"] != asset:
        raise ReviewError("所选分析属于另一个资产。")
    st.info(f"分析 {result['status']} · 索引 {result['index_state']}")
    job = st.session_state.get("job_id")
    if job:
        status = client.request("GET", "/v1/jobs/" + identifier(job)).json()
        if status["analysis_id"] == analysis:
            st.caption(f"作业 {job} · 已记录模型尝试 {status['progress']['model_attempts']}")
            if status.get("error_code"):
                st.warning(status["error_code"])
            if status["status"] in ("running", "queued") and st.button("取消分析"):
                client.request("POST", f"/v1/jobs/{job}/cancel")
                st.info("取消请求已记录。")
    st.header("证据时间轴")
    evidence: list[str] = []
    for row in result["timeline"]:
        st.text(f"{row['start_ms']}–{row['end_ms']} ms · {row['kind']} · {row['text']}")
        evidence.extend(row["evidence_ids"])
    back, more = st.columns(2)
    if back.button("上一页", disabled=st.session_state.offset == 0):
        st.session_state.offset = max(0, st.session_state.offset - 20)
        st.rerun()
    if more.button("下一页", disabled=result["next_offset"] is None):
        st.session_state.offset = result["next_offset"]
        st.rerun()
    st.header("提问")
    question = st.text_input("问题", key="question_text").strip()
    qmode = st.selectbox("问题类型", ["qa", "event_localization"])
    if st.button(
        "提交问题",
        key="ask",
        disabled=result["index_state"] not in ("ready", "partial"),
    ):
        if not question:
            raise ReviewError("请输入问题。")
        ids = client.submit(
            f"/v1/analyses/{analysis}/questions",
            {"query_text": question, "mode": qmode},
            _intent("question_intent", (analysis, question, qmode)),
        )
        st.session_state.update(question_id=ids["question_id"], qa_job_id=ids["qa_job_id"])
        del st.session_state["question_intent"]
    if qid := st.session_state.get("question_id"):
        value = client.request("GET", "/v1/questions/" + identifier(qid)).json()
        st.info(f"问题 {value['status']}")
        if answer := value.get("answer"):
            st.markdown(f"**回答状态** {answer['status']}")
            for claim in answer["claims"]:
                st.text(claim["text"])
                st.caption(claim["support_kind"] + " · 引用 " + "、".join(claim["evidence_ids"]))
                for limitation in claim["limitations"]:
                    st.text(limitation)
                evidence.extend(claim["evidence_ids"])
            for gap in answer["gaps"]:
                st.text(gap)
        elif value["status"] in ("failed", "cancelled"):
            job_info = client.request(
                "GET", "/v1/jobs/" + identifier(st.session_state.qa_job_id)
            ).json()
            st.warning(job_info.get("error_code") or "未生成回答。")
        elif st.button("取消问题"):
            client.request("POST", "/v1/jobs/" + identifier(st.session_state.qa_job_id) + "/cancel")
            st.info("取消请求已记录。")
    if not evidence:
        return
    eid = st.selectbox("查看引用", list(dict.fromkeys(evidence)))
    meta = client.evidence(analysis, eid, "meta").json()
    item = meta["evidence"]
    if meta["kind"] == "transcript":
        span = item["time_range"]
        st.caption(f"原始语音时间 {span['start_ms']}–{span['end_ms']} ms")
        st.text(item["text"])
        return
    st.image(client.evidence(analysis, eid, "frame").content)
    st.caption(
        f"实际帧时间 {item['actual_pts_ms']} ms · 请求 {item['requested_ms']} ms"
        f" · PTS {item['raw_pts']}"
    )
    mapping = PlaybackTimelineMap.model_validate(meta["playback_timeline_map"])
    # The native browser player cannot attach the local API's Bearer header.
    if (
        playback_seconds(
            mapping,
            asset_id=meta["asset_id"],
            artifact_id=mapping.artifact_id,
            actual_ms=item["actual_pts_ms"],
            authenticated_source=False,
        )
        is None
    ):
        reason = (
            "播放映射尚未验证"
            if mapping.status == "preview_only"
            else "浏览器播放器无法携带本机鉴权"
        )
        st.caption(f"精确帧预览（preview_only）· {reason}。")


def render() -> None:
    st.set_page_config(page_title="视频证据核对", layout="centered")
    st.title("视频证据核对")
    try:
        with closing(
            ReviewClient(
                os.environ.get("VIDEO_AGENT_API_URL", "http://127.0.0.1:8000"),
                os.environ.get("VIDEO_AGENT_TOKEN", ""),
            )
        ) as client:
            _review(client)
    except ReviewError as exc:
        st.error(str(exc))


if __name__ == "__main__":
    render()
