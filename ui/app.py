import hashlib
import os

import httpx
import streamlit as st

API = os.getenv("RESEARCHPILOT_API_URL", "http://127.0.0.1:8000").rstrip("/")


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or f"请求失败（HTTP {response.status_code}）"
    detail = payload.get("detail") or payload.get("error") or payload
    if isinstance(detail, dict):
        blockers = detail.get("blockers") or []
        return "；".join([detail.get("message", "请求失败"), *blockers])
    return str(detail)


@st.cache_data(ttl=3, max_entries=128, show_spinner=False)
def get_json(path: str, paper: str | None = None, query: dict | None = None):
    params = {"paper": paper} if paper else {}
    if query:
        params.update(query)
    response = httpx.get(f"{API}{path}", params=params, timeout=30)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=300, max_entries=8, show_spinner=False)
def get_pdf(path: str) -> bytes:
    response = httpx.get(f"{API}{path}", timeout=60)
    response.raise_for_status()
    return response.content


def mutate(path: str, *, method: str = "POST", json=None, data=None, files=None):
    try:
        response = httpx.request(
            method,
            f"{API}{path}", json=json, data=data, files=files, timeout=300
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        st.error(_error_message(exc.response))
        return None
    except httpx.HTTPError as exc:
        st.error(f"无法连接 ResearchPilot：{exc}")
        return None
    get_json.clear()
    get_pdf.clear()
    return response.json() if response.content else {}


def text_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def step_header(stage: str, project_id: str | None = None) -> int:
    active = {"setup": 1, "searching": 2, "paper_selection": 3,
              "acquiring_selected": 4, "documents_needed": 4,
              "analyzing_selected": 4, "paused": 4,
              "analysis_review": 4,
              "failed": 4}.get(stage, 1)
    labels = ("描述课题", "检索文献", "选择论文", "证据化分析")
    state_key = f"workflow-view-{project_id}" if project_id else None
    if state_key:
        active = st.session_state.setdefault(state_key, active)
    columns = st.columns(4)
    for index, label in enumerate(labels, 1):
        if columns[index - 1].button(
            f"{index}. {label}",
            key=f"stage-{project_id or 'new'}-{index}",
            type="primary" if index == active else "secondary",
            width="stretch",
            disabled=project_id is None,
        ):
            active = index
            st.session_state[state_key] = index
    return active


def submit_action(project_id: str, payload: dict) -> dict | None:
    result = mutate(f"/projects/{project_id}/actions", json=payload)
    if result:
        st.toast(result["message"])
    return result


@st.dialog("删除课题", icon=":material/delete:", on_dismiss="rerun")
def confirm_project_deletion(
    project_id: str, project_name: str, remaining_project_ids: list[str]
) -> None:
    st.warning(f"确定删除“{project_name}”吗？课题记录、检索结果和本地文件都无法恢复。")
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("取消", key=f"cancel-delete-{project_id}"):
            st.rerun()
        if st.button(
            "确认删除",
            key=f"confirm-delete-{project_id}",
            type="primary",
            icon=":material/delete_forever:",
        ) and mutate(f"/projects/{project_id}", method="DELETE") is not None:
            if st.session_state.project_id == project_id:
                st.session_state.project_id = (
                    remaining_project_ids[0] if remaining_project_ids else None
                )
            st.session_state.creating_new = not bool(remaining_project_ids)
            st.toast("课题已删除")
            st.rerun()


def render_setup(health: dict) -> None:
    st.subheader("描述你的研究课题")
    st.caption("ResearchPilot 会先检索论文，等你选定一至两篇后，再阅读正文和图表并生成证据化分析。")
    capability = health.get("paper_analysis", {})
    if capability.get("ready"):
        st.success(f"本地视觉模型已就绪：{capability.get('vision_model')}")
    else:
        st.warning("新项目暂时无法启动。" + "；".join(capability.get("blockers", [])))
        if st.button("重新检测模型", icon=":material/refresh:"):
            get_json.clear()
            st.rerun()
    with st.form("new_project"):
        project_name = st.text_input("课题名称", placeholder="用于左侧课题列表显示")
        question = st.text_area("研究问题", placeholder="你希望通过论文研究解决或验证什么？")
        approach = st.text_area("当前方案（可选）", placeholder="目前采用的方法、模型或实验设置")
        difficulties = st.text_area("主要困难", placeholder="每行一个问题")
        metrics = st.text_area("目标指标", placeholder="每行一个指标")
        with st.expander("高级设置"):
            year_from = st.number_input("起始年份", 1900, 2100, 2020)
            year_to = st.number_input("结束年份", 1900, 2100, 2026)
            maximum_papers = st.slider("最多显示检索结果数", 1, 50, 15)
            sources = st.multiselect(
                "检索来源", ["openalex", "crossref", "arxiv"],
                default=["openalex", "crossref", "arxiv"],
            )
        submitted = st.form_submit_button(
            "创建并开始论文研究", type="primary", icon=":material/search:",
            disabled=not capability.get("ready", False),
        )
    if not submitted:
        return
    result = mutate("/projects", json={
        "project_name": project_name.strip() or None,
        "research_question": question.strip(),
        "current_approach": approach.strip() or None,
        "difficulties": text_lines(difficulties),
        "target_metrics": text_lines(metrics),
        "advanced": {"year_from": year_from, "year_to": year_to,
                     "max_papers": maximum_papers, "sources": sources},
    })
    if result:
        st.session_state.project_id = result["workspace"]["project_id"]
        st.session_state.creating_new = False
        st.rerun()


def render_project_setup(project_id: str, workspace: dict) -> None:
    value = workspace.get("project_input") or {}
    advanced = value.get("advanced") or {}
    st.subheader("编辑课题信息")
    st.caption("保存后前往“检索文献”重新检索；仅修改名称时也不会自动丢弃现有结果。")
    with st.form(f"edit-project-{project_id}"):
        project_name = st.text_input("课题名称", value=value.get("project_name") or workspace["name"])
        question = st.text_area("研究问题", value=value.get("research_question") or "")
        approach = st.text_area("当前方案（可选）", value=value.get("current_approach") or "")
        difficulties = st.text_area(
            "主要困难", value="\n".join(value.get("difficulties") or [])
        )
        metrics = st.text_area(
            "目标指标", value="\n".join(value.get("target_metrics") or [])
        )
        with st.expander("高级设置"):
            year_from = st.number_input(
                "起始年份", 1900, 2100, int(advanced.get("year_from") or 2020)
            )
            year_to = st.number_input(
                "结束年份", 1900, 2100, int(advanced.get("year_to") or 2026)
            )
            maximum_papers = st.slider(
                "最多显示检索结果数", 1, 50, int(advanced.get("max_papers") or 15)
            )
            sources = st.multiselect(
                "检索来源", ["openalex", "crossref", "arxiv"],
                default=advanced.get("sources") or ["openalex", "crossref", "arxiv"],
            )
        submitted = st.form_submit_button(
            "保存课题信息", type="primary", icon=":material/save:"
        )
    if submitted and mutate(f"/projects/{project_id}", method="PATCH", json={
        "project_name": project_name.strip(),
        "research_question": question.strip(),
        "current_approach": approach.strip() or None,
        "difficulties": text_lines(difficulties),
        "target_metrics": text_lines(metrics),
        "advanced": {"year_from": year_from, "year_to": year_to,
                     "max_papers": maximum_papers, "sources": sources},
    }) is not None:
        st.toast("课题信息已保存")
        st.rerun()


def render_documents(project_id: str, action: dict) -> None:
    st.warning("以下关键论文缺少全文，请上传你有权使用的 PDF。")
    for document in action.get("documents", []):
        with st.container(border=True):
            st.markdown(f"**{document['title']}**" +
                        (f"（{document['year']}）" if document.get("year") else ""))
            if document.get("reason"):
                st.caption(f"自动获取结果：{document['reason']}")
            uploaded = st.file_uploader(
                f"上传 {document['title']}", type=["pdf"],
                key=f"upload-{document['upload_token']}", label_visibility="collapsed",
            )
            if uploaded and st.button(
                "上传并继续", key=f"submit-{document['upload_token']}",
                type="primary", icon=":material/upload_file:",
            ):
                files = [("files", (uploaded.name, uploaded.getvalue(), "application/pdf"))]
                if mutate(f"/projects/{project_id}/documents",
                          data={"upload_token": document["upload_token"]}, files=files):
                    st.rerun()


def render_source_card(
    project_id: str, title: str, content, resource_url: str, evidence: dict | None = None
) -> None:
    with st.container(border=True):
        st.markdown(title)
        if content:
            st.write(content)
        with st.container(horizontal=True):
            st.link_button(
                "查看来源", f"{API}{resource_url}", icon=":material/open_in_new:"
            )
            if evidence:
                for label, review in (("确认", "confirmed"), ("存疑", "doubted"),
                                      ("排除", "excluded")):
                    if st.button(label, key=f"{review}-{evidence['evidence_token']}"):
                        submit_action(project_id, {
                            "type": "evidence_review",
                            "evidence_token": evidence["evidence_token"],
                            "status": review,
                        })
                        st.rerun()


def render_research_materials(project_id: str, workspace: dict) -> None:
    st.subheader("研究资料")
    papers = workspace.get("literature", [])
    if not papers:
        st.caption("论文卡片将在检索完成后显示。")
        return
    query = st.text_input("筛选论文", placeholder="输入标题或作者", key="paper_filter")
    filtered = [paper for paper in papers if query.casefold() in (
        paper["title"] + " " + " ".join(paper.get("authors", []))).casefold()]
    labels = {paper["paper_token"]: f"{paper['title']} · {paper.get('year') or '年份未知'}"
              for paper in filtered}
    if not labels:
        st.info("没有匹配的论文。")
        return
    selected = st.selectbox("选择论文", list(labels), format_func=labels.get,
                            label_visibility="collapsed", key="selected_paper")
    detail = get_json(f"/projects/{project_id}/workspace", selected)
    paper = detail.get("selected_paper") or {}
    with st.container(border=True):
        st.markdown(f"### {paper.get('title', labels[selected])}")
        metadata = paper.get("metadata") or {}
        st.caption("来源：" + "、".join(metadata.get("sources") or [metadata.get("source", "未知")]))
        st.caption(_paper_provenance(metadata))
        if metadata.get("abstract"):
            st.write(metadata["abstract"])
    figures = paper.get("figures", [])
    tables = paper.get("tables", [])
    if figures or tables:
        st.markdown("#### 图表分析")
    for kind, cards in (("图", figures), ("表", tables)):
        for index, card in enumerate(cards, start=1):
            analysis = card.get("analysis") or {}
            render_source_card(
                project_id,
                f"**{card.get('label') or f'{kind}表区域 {index}'} · 第 {card.get('page')} 页**",
                analysis.get("observations") or analysis.get("direct_content"),
                card["resource_url"],
            )
    evidence = paper.get("evidence", [])
    if evidence:
        st.markdown("#### 可追溯证据")
    for item in evidence:
        render_source_card(
            project_id,
            f"**第 {item['page']} 页 · {item['type']} · 置信度 {item['confidence']:.0%}**",
            item["claim"],
            item["resource_url"],
            item,
        )


def render_search_plan(workspace: dict) -> None:
    plan = workspace.get("search_plan") or {}
    st.subheader("检索策略")
    if not plan:
        st.caption("尚未生成检索策略。")
        return
    understanding = plan.get("understanding") or {}
    if understanding.get("normalized_goal"):
        st.write(understanding["normalized_goal"])
    strategy = plan.get("strategy") or {}
    if strategy.get("required_concept_groups"):
        st.markdown("**必要概念组**")
        for group in strategy["required_concept_groups"]:
            st.write(" · ".join(group))
    queries = plan.get("queries") or []
    if queries:
        st.markdown("**检索词**")
        for query in queries:
            value = query.get("query", query) if isinstance(query, dict) else query
            purpose = query.get("purpose") if isinstance(query, dict) else None
            st.code(f"{value}" + (f"  [{purpose}]" if purpose else ""))
    st.caption(
        f"来源：{', '.join(plan.get('sources') or [])} · "
        f"年份：{plan.get('year_from') or '不限'}–{plan.get('year_to') or '不限'}"
    )


def render_search_stage(project_id: str, workspace: dict) -> None:
    render_search_plan(workspace)
    with st.form(f"restart-search-{project_id}"):
        instruction = st.text_area(
            "本轮检索补充要求（可选）",
            value=(workspace.get("search_plan") or {}).get("instruction") or "",
            placeholder="例如：聚焦近三年的跨模态配准方法，并排除综述论文",
        )
        restart = st.form_submit_button(
            "重新检索", type="primary", icon=":material/search:"
        )
    if restart:
        payload = {"type": "regenerate_search"}
        if instruction.strip():
            payload["instruction"] = instruction.strip()
        if submit_action(project_id, payload):
            st.session_state[f"workflow-view-{project_id}"] = 2
            st.rerun()


def _paper_provenance(paper: dict) -> str:
    """Journal + publisher line for a paper card, with a preprint fallback."""
    venue = (paper.get("venue") or "").strip()
    publisher = (paper.get("publisher") or "").strip()
    parts = []
    if venue:
        parts.append(f"期刊/会议：{venue}")
    if publisher:
        parts.append(f"出版社：{publisher}")
    if parts:
        return " · ".join(parts)
    if "arxiv" in (paper.get("sources") or []):
        return "arXiv 预印本（来源未提供期刊与出版社）"
    return "来源未提供期刊与出版社"


_FULL_TEXT_BADGES = {
    "direct": (":material/download_done:", "green"),
    "likely": (":material/download:", "blue"),
    "uncertain": (":material/help:", "orange"),
    "manual": (":material/upload_file:", "gray"),
}

def _render_full_text_hint(paper: dict) -> None:
    """Badge the pre-selection guess at whether the PDF can be auto-downloaded.

    Falls back to the post-download status once an acquisition exists, because
    that is a fact rather than a guess. The caption states plainly that the
    badge is a prediction, since only a real download settles the question.
    """
    hint = paper.get("full_text_hint") or {}
    status = paper.get("full_text")
    if status:
        st.caption(
            {
                "parsed": "全文状态：已获取并解析",
                "awaiting_upload": "全文状态：自动获取失败，等待手动上传 PDF",
                "downloading": "全文状态：正在下载",
                "pending": "全文状态：排队等待获取",
                "failed": "全文状态：获取失败",
            }.get(status, f"全文状态：{status}")
        )
        return
    if not hint:
        return
    icon, color = _FULL_TEXT_BADGES.get(hint.get("level"), (":material/help:", "gray"))
    st.badge(hint.get("label") or "获取情况未知", icon=icon, color=color)
    if hint.get("detail"):
        st.caption(hint["detail"])


def render_paper_selection(project_id: str, workspace: dict) -> None:
    if not workspace.get("search_plan"):
        st.info("请先前往“检索文献”完成一次检索。")
        return

    papers = workspace.get("literature") or []
    st.subheader(f"选择论文（当前结果 {len(papers)} 篇）")

    def _hint_level(paper: dict) -> str | None:
        return (paper.get("full_text_hint") or {}).get("level")

    direct = sum(1 for paper in papers if _hint_level(paper) == "direct")
    parse_needed = sum(1 for paper in papers if _hint_level(paper) == "likely")
    manual_risk = sum(1 for paper in papers if _hint_level(paper) in {"uncertain", "manual"})
    if direct or parse_needed or manual_risk:
        st.caption(
            f"可直接获取 {direct} 篇 · 需跳转解析 {parse_needed} 篇 · "
            f"可能需手动上传 {manual_risk} 篇。"
            "标注由元数据推断，仅供参考：即便标为“可直接获取”，"
            "目标站点仍可能拦截自动下载，失败时会提示手动上传 PDF。"
        )
    filter_text = st.text_input("筛选标题或作者", key="selection_filter")
    visible = [paper for paper in papers if filter_text.casefold() in (
        paper["title"] + " " + " ".join(paper.get("authors", []))
    ).casefold()]
    labels = {
        paper["paper_token"]: f"{paper['title']} · {paper.get('year') or '年份未知'}"
        for paper in visible
    }
    defaults = [token for token in workspace.get("selected_paper_tokens", []) if token in labels]
    with st.form("select_papers"):
        selected = st.multiselect(
            "选择一至两篇论文", list(labels), default=defaults, format_func=labels.get,
            max_selections=2,
        )
        requirements = st.text_area(
            "本次重点分析要求（可选）",
            value=workspace.get("analysis_requirements") or "",
            placeholder="例如：重点比较图表理解机制、数据集和评价指标",
        )
        confirmed = st.form_submit_button(
            "确认论文并开始分析", type="primary", icon=":material/check:"
        )
    for paper in visible:
        # Journal / publisher stay outside the expander: a collapsed Streamlit
        # header can ellipsize a long title, which would hide the provenance.
        with st.container(border=True):
            st.markdown(f"**{paper['title']}**")
            if paper.get("reason"):
                st.caption(f"相关性说明：{paper['reason']}")
            st.caption(_paper_provenance(paper))
            authors = "、".join(paper.get("authors", []))
            if authors:
                st.caption(authors)
            _render_full_text_hint(paper)
            with st.expander("摘要"):
                st.write(paper.get("abstract") or "暂无摘要")
    if confirmed:
        if not selected:
            st.warning("请先选择一至两篇论文。")
        elif submit_action(project_id, {
                "type": "select_papers", "paper_tokens": selected,
                "analysis_requirements": requirements.strip() or None,
            }):
            st.rerun()

def _render_inline_evidence(evidence: dict | str, index: int) -> None:
    if isinstance(evidence, str):
        st.link_button(f"查看证据 {index}", f"{API}{evidence}")
        return
    page = evidence.get("page")
    label = evidence.get("label") or evidence.get("section")
    source = " · ".join(filter(None, [f"第 {page} 页" if page else None, label]))
    if evidence.get("type") in {"figure", "table"}:
        st.image(
            f"{API}{evidence['resource_url']}",
            caption=source or "论文图表证据",
            width="stretch",
        )
        if evidence.get("claim"):
            st.caption(f"视觉观察：{evidence['claim']}")
    else:
        st.caption(source or "论文正文证据")
        st.markdown(f"> {evidence.get('excerpt') or evidence.get('claim') or '无可用摘录'}")


def _render_claims(title: str, claims: list[dict], *, numbered: bool = False) -> None:
    if not claims:
        st.caption(f"{title}：论文未提供足够信息。")
        return
    st.markdown(f"#### {title}")
    for claim_index, claim in enumerate(claims, 1):
        marker = "证据支持" if claim["kind"] == "supported" else "推断"
        with st.container(border=True):
            prefix = f"**{claim_index}.** " if numbered else ""
            st.markdown(f"{prefix}{claim['value']}")
            evidence_values = claim.get("evidence", [])
            detail = f"{marker} · {len(evidence_values)} 条来源"
            if evidence_values:
                with st.expander(detail):
                    for evidence_index, evidence in enumerate(evidence_values, 1):
                        _render_inline_evidence(evidence, evidence_index)
            else:
                st.caption(detail)


def _render_overview(claim: dict | None) -> None:
    if claim:
        with st.container(border=True):
            st.markdown("#### 综合概述")
            st.markdown(claim["value"])
            evidence_values = claim.get("evidence", [])
            if evidence_values:
                with st.expander(f"查看概述依据 · {len(evidence_values)} 条来源"):
                    for evidence_index, evidence in enumerate(evidence_values, 1):
                        _render_inline_evidence(evidence, evidence_index)


def _render_report_content(report: dict) -> None:
    st.subheader("证据化综合分析")
    if report.get("analysis_requirements"):
        st.info(f"分析重点：{report['analysis_requirements']}")
    for paper in report.get("papers", []):
        with st.container(border=True):
            st.markdown(f"### {paper['title']}")
            all_claims = [
                claim
                for field in (
                    "core_problem", "methods", "mechanisms", "experimental_setup",
                    "main_results", "limitations", "relevance_to_topic",
                )
                for claim in paper.get(field, [])
            ]
            supported = sum(claim.get("kind") == "supported" for claim in all_claims)
            evidence_count = len({
                item.get("resource_url")
                for claim in all_claims
                for item in claim.get("evidence", [])
                if isinstance(item, dict) and item.get("resource_url")
            })
            metrics = st.columns(3)
            metrics[0].metric("分析要点", len(all_claims), border=True)
            metrics[1].metric("证据支持", supported, border=True)
            metrics[2].metric("引用来源", evidence_count, border=True)
            _render_overview(paper.get("overview"))
            problem_tab, method_tab, experiment_tab, critical_tab = st.tabs([
                "问题与贡献", "方法与机制", "实验与结果", "局限与相关性",
            ])
            with problem_tab:
                _render_claims("核心问题", paper.get("core_problem", []))
            with method_tab:
                _render_claims("方法流程", paper.get("methods", []), numbered=True)
                _render_claims("作用机制", paper.get("mechanisms", []), numbered=True)
            with experiment_tab:
                _render_claims("实验设置", paper.get("experimental_setup", []))
                _render_claims("主要结果", paper.get("main_results", []))
            with critical_tab:
                _render_claims("局限与适用条件", paper.get("limitations", []))
                _render_claims("与课题的关系", paper.get("relevance_to_topic", []))
    comparison = report.get("comparison")
    if comparison:
        st.markdown("### 两篇论文对比")
        _render_overview(comparison.get("overview"))
        for field, title in (
            ("commonalities", "共同点"), ("differences", "差异"),
            ("complementarities", "互补性"), ("applicability", "适用条件"),
        ):
            _render_claims(title, comparison.get(field, []))


_CLAIM_FIELD_TITLES = {
    "core_problem": "核心问题",
    "relevance_to_topic": "与课题的关系",
    "methods": "方法流程",
    "mechanisms": "作用机制",
    "experimental_setup": "实验设置",
    "main_results": "主要结果",
    "limitations": "局限与适用条件",
    "overview": "综合概述",
}


def _render_block_annotate(project_id: str, workspace: dict, block: dict) -> None:
    note = block.get("note")
    if note:
        st.info(f"当前批注：{note}")
    note_key = f"block-note-{project_id}-{block['paper_id']}-{block['part_key']}"
    with st.popover("批注并重新分析", icon=":material/edit_note:"):
        value = st.text_area(
            "你的批注 / 补充要求",
            value=block.get("note") or "",
            key=note_key,
            placeholder="例如：方法部分忽略了消融实验，请补充并说明对照组设置。",
        )
        st.caption(
            "重新分析将以该「板块」为单位重跑（其余板块复用缓存），完成后会重新生成综合报告。"
        )
        if st.button(
            "保存批注并重新分析",
            key=f"block-submit-{note_key}",
            type="primary",
            icon=":material/tune:",
        ):
            if not value.strip():
                st.warning("请先填写批注内容。")
            elif submit_action(project_id, {
                "type": "reanalyze_part",
                "paper_token": block["paper_token"],
                "part": block["part_key"],
                "instruction": value.strip(),
            }):
                st.rerun()


def _render_analysis_block(project_id: str, workspace: dict, block: dict) -> None:
    status = block.get("status") or "queued"
    label = block.get("part_label") or block["part_key"]
    content = block.get("content")
    if status == "completed" and content:
        if block["part_key"] == "overview":
            _render_overview(content.get("overview"))
        else:
            for field, claims in content.items():
                _render_claims(
                    _CLAIM_FIELD_TITLES.get(field, field), claims,
                    numbered=field in {"methods", "mechanisms"},
                )
        _render_block_annotate(project_id, workspace, block)
    elif status == "running":
        with st.container(border=True):
            st.markdown(f"**{label}**")
            st.caption("正在分析…")
    else:
        with st.container(border=True):
            st.markdown(f"**{label}**")
            st.caption("待分析")


def _render_paper_blocks(project_id: str, workspace: dict, papers: list[dict]) -> None:
    blocks_by_paper: dict[str, list[dict]] = {}
    for block in workspace.get("analysis_blocks") or []:
        blocks_by_paper.setdefault(block["paper_id"], []).append(block)
    field_order = (
        "core_problem", "methods", "mechanisms", "experimental_setup",
        "main_results", "limitations", "relevance_to_topic",
    )
    for paper in papers:
        pid = paper["paper_id"]
        paper_blocks = blocks_by_paper.get(pid, [])
        with st.container(border=True):
            st.markdown(f"### {paper['title']}")
            if not paper_blocks:
                st.caption("分析内容尚未生成。")
                continue
            total_claims = 0
            supported = 0
            evidence_urls = set()
            for block in paper_blocks:
                if block.get("status") != "completed":
                    continue
                content = block.get("content") or {}
                for field in field_order:
                    for claim in content.get(field, []):
                        total_claims += 1
                        if claim.get("kind") == "supported":
                            supported += 1
                        for item in claim.get("evidence") or []:
                            if item.get("resource_url"):
                                evidence_urls.add(item["resource_url"])
            metrics = st.columns(3)
            metrics[0].metric("分析要点", total_claims, border=True)
            metrics[1].metric("证据支持", supported, border=True)
            metrics[2].metric("引用来源", len(evidence_urls), border=True)
            overview = next(
                (block for block in paper_blocks if block["part_key"] == "overview"), None
            )
            if overview:
                _render_analysis_block(project_id, workspace, overview)
            tabs = st.tabs(["问题与贡献", "方法与机制", "实验与结果", "局限与相关性"])
            for tab, key in zip(tabs, ("problem", "method", "experiment", "critical")):
                with tab:
                    block = next(
                        (item for item in paper_blocks if item["part_key"] == key), None
                    )
                    if block:
                        _render_analysis_block(project_id, workspace, block)
                    else:
                        st.caption("该部分尚未开始。")


def render_analysis_workspace(project_id: str, workspace: dict) -> None:
    high_risk = workspace.get("high_risk_review_count", 0)
    if high_risk:
        st.info(
            f"有 {high_risk} 条自动存疑证据被结论引用，建议先到“证据复核”处理后再重新生成报告。"
        )
    view = st.segmented_control(
        "视图", options=("综合分析", "证据复核"), default="综合分析",
        key=f"analysis-view-{project_id}",
    )
    if view == "证据复核":
        render_review_desk(project_id, workspace)
        return
    report = workspace.get("analysis_report") or {}
    papers = workspace.get("analysis_papers") or []
    if not papers and report:
        papers = [
            {
                "paper_id": paper.get("paper_id"),
                "paper_token": paper.get("paper_token"),
                "title": paper.get("title"),
                "pdf_url": paper.get("pdf_url"),
            }
            for paper in report.get("papers", [])
        ]
    state = workspace["user_stage"]
    if papers:
        analysis_column, pdf_column = st.columns([3, 2], gap="medium")
        with analysis_column:
            _render_paper_blocks(project_id, workspace, papers)
            comparison = report.get("comparison")
            if comparison:
                st.markdown("### 两篇论文对比")
                _render_overview(comparison.get("overview"))
                for field, title in (
                    ("commonalities", "共同点"), ("differences", "差异"),
                    ("complementarities", "互补性"), ("applicability", "适用条件"),
                ):
                    _render_claims(title, comparison.get(field, []))
        with pdf_column:
            _render_pdf_reader(papers)
    else:
        st.info("已选定论文；获取全文后将在此显示原文与逐块分析。")
    if state in {"acquiring_selected", "analyzing_selected"}:
        render_live_analysis_controls(project_id, workspace)
    elif state == "paused":
        render_paused_analysis(project_id, workspace)
    elif report:
        if st.button(
            "基于现有证据重新生成详细分析",
            icon=":material/refresh:",
            help="保留已解析的 PDF、图表和证据，只重新生成更完整的分析报告；被排除的证据将不再被引用。",
        ) and submit_action(project_id, {"type": "reanalyze_selected"}):
            st.rerun()
        render_partial_reanalysis(project_id, workspace)
        if workspace.get("analysis_board"):
            with st.expander("各阶段完成情况", expanded=False):
                render_analysis_board(workspace)


def _render_pdf_reader(papers: list[dict]) -> None:
    papers = [paper for paper in papers if paper.get("pdf_url")]
    with st.container(border=True):
        st.subheader("论文原文")
        if not papers:
            st.info("当前没有可显示的 PDF 全文。")
            return
        labels = {paper["pdf_url"]: paper["title"] for paper in papers}
        selected = st.selectbox(
            "切换论文",
            list(labels),
            format_func=labels.get,
            key="analysis_pdf",
            label_visibility="collapsed" if len(papers) == 1 else "visible",
        )
        try:
            pdf = get_pdf(selected)
        except httpx.HTTPError as exc:
            st.error(f"无法加载 PDF 原文：{exc}")
            return
        # CCv2 reserves ``__`` inside component IDs, so keep the URL out of the
        # key while retaining a stable, distinct viewer identity for each PDF.
        pdf_key = hashlib.sha256(selected.encode("utf-8")).hexdigest()[:16]
        st.pdf(pdf, height=1050, key=f"analysis-pdf-{pdf_key}")


_DESK_STATUS_LABEL = {"confirmed": "已确认", "doubted": "存疑", "excluded": "已排除"}
_DESK_SOURCE_LABEL = {
    "auto_visual_verifier": "图表自动复核",
    "auto_consistency": "图文一致性",
    "human": "人工判定",
    "auto": "自动复核",
}
_DESK_TYPE_LABEL = {"figure": "图", "table": "表", "text": "正文"}


def _desk_badge(item: dict) -> str:
    status = item.get("review_status")
    if not status:
        return ":gray[待复核]"
    color = {"confirmed": "green", "doubted": "orange", "excluded": "red"}.get(
        status, "gray"
    )
    who = _DESK_SOURCE_LABEL.get(item.get("review_source") or "auto", "自动复核")
    return f":{color}[{who} · {_DESK_STATUS_LABEL[status]}]"


def _desk_commit(project_id: str, item: dict, status: str, impact: dict | None = None):
    result = submit_action(project_id, {
        "type": "evidence_review",
        "evidence_token": item["evidence_token"],
        "status": status,
    })
    if result is None:
        return
    summary = st.session_state[f"review-session-{project_id}"]
    summary[status] += 1
    if status == "excluded" and impact:
        summary["cited_total"] += int(impact.get("cited_total", 0))
        summary["downgrade"] += len(impact.get("downgrade") or [])
        summary["retained"] += len(impact.get("retained") or [])
    st.rerun()


def _render_desk_item(project_id: str, item: dict) -> None:
    token = item["evidence_token"]
    pending_key = f"desk-exclude-pending-{project_id}-{token}"
    kind = item.get("type", "text")
    label = item.get("label")
    header = f"**{_DESK_TYPE_LABEL.get(kind, kind)}** "
    if label:
        header += f"{label} · "
    header += f"第 {item['page']} 页 · 置信度 {item['confidence']:.0%} · {_desk_badge(item)}"
    with st.container(border=True):
        st.markdown(header)
        if kind in {"figure", "table"}:
            st.image(
                f"{API}{item['resource_url']}",
                caption=item.get("claim") or "论文图表证据",
                width="stretch",
            )
        else:
            quote = item.get("excerpt") or item.get("claim") or "无可用摘录"
            st.markdown(f"> {quote}")
        note = item.get("review_note")
        if note:
            who = _DESK_SOURCE_LABEL.get(item.get("review_source") or "auto", "自动复核")
            st.caption(f"{who}判定：{note}")
        cited_by = item.get("cited_by", 0)
        comparison_hint = "，含双篇比较结论" if item.get("supports_comparison") else ""
        st.caption(f"被 {cited_by} 条结论引用{comparison_hint} · 决策价值 {item.get('priority', 0):.2f}")
        citing = item.get("citing") or []
        if citing:
            with st.expander(f"引用它的结论 · {len(citing)} 条", expanded=False):
                for claim in citing:
                    marker = " :red[(唯一来源，排除后降级为推断)]" if claim.get("will_downgrade") else ""
                    st.markdown(
                        f"- **{claim.get('paper_title')} · {claim.get('section_label')}**："
                        f"{claim.get('value')}{marker}"
                    )
        pending = st.session_state.get(pending_key)
        if pending is None:
            first, second, third = st.columns(3)
            if first.button("确认", key=f"desk-confirm-{token}", icon=":material/check:", width="stretch"):
                _desk_commit(project_id, item, "confirmed")
            if second.button("存疑", key=f"desk-doubt-{token}", icon=":material/help:", width="stretch"):
                _desk_commit(project_id, item, "doubted")
            if third.button(
                "排除", key=f"desk-exclude-{token}",
                icon=":material/block:", width="stretch",
            ):
                preview = mutate(f"/projects/{project_id}/review-desk/preview", json={
                    "evidence_token": token, "status": "excluded",
                })
                if preview is not None:
                    st.session_state[pending_key] = preview
                    st.rerun()
        else:
            st.info(pending.get("message") or "请确认是否排除该证据")
            confirm_column, cancel_column = st.columns(2)
            if confirm_column.button(
                "确认排除", key=f"desk-exclude-confirm-{token}", type="primary",
                icon=":material/block:", width="stretch",
            ):
                st.session_state.pop(pending_key, None)
                _desk_commit(project_id, item, "excluded", pending)
            if cancel_column.button(
                "取消", key=f"desk-exclude-cancel-{token}", width="stretch"
            ):
                st.session_state.pop(pending_key, None)
                st.rerun()


def render_review_desk(project_id: str, workspace: dict) -> None:
    st.subheader("证据复核决策台")
    # The desk builds a citation index from the report, or (at the
    # pre-synthesis review gate) from the persisted per-paper analyses. It is
    # only meaningful once something exists to review; before that the endpoint
    # answers 409, so surface a friendly note instead of an error.
    report_present = bool(workspace.get("analysis_report"))
    analyses_done = any(
        block.get("status") == "completed"
        for block in (workspace.get("analysis_blocks") or [])
    )
    if not report_present and not analyses_done:
        st.info("分析报告尚未生成，暂无证据可复核（将在综合报告生成后开放）。")
        return
    session_key = f"review-session-{project_id}"
    summary = st.session_state.setdefault(
        session_key,
        {"confirmed": 0, "doubted": 0, "excluded": 0,
         "cited_total": 0, "downgrade": 0, "retained": 0},
    )
    if any(summary.values()):
        columns = st.columns(5)
        columns[0].metric("本轮确认", summary["confirmed"], border=True)
        columns[1].metric("本轮存疑", summary["doubted"], border=True)
        columns[2].metric("本轮排除", summary["excluded"], border=True)
        columns[3].metric("受影响结论", summary["cited_total"], border=True)
        if columns[4].button(
            "重置本轮", icon=":material/restart_alt:", key=f"review-reset-{project_id}"
        ):
            st.session_state[session_key] = {
                "confirmed": 0, "doubted": 0, "excluded": 0,
                "cited_total": 0, "downgrade": 0, "retained": 0,
            }
            st.rerun()
        if summary["excluded"]:
            st.warning(
                f"本轮已排除 {summary['excluded']} 条证据：报告仍基于旧证据。请切到"
                "“综合分析”页并点击“基于现有证据重新生成详细分析”，被排除证据将不再被引用。"
            )
    else:
        st.caption("本轮暂无操作：排除会先回显影响，提交后在此累计量化产出。")
    segment = st.segmented_control(
        "待审范围",
        options=("优先处理", "全部"),
        default="优先处理",
        key=f"review-segment-{project_id}",
    )
    try:
        desk = get_json(
            f"/projects/{project_id}/review-desk",
            query={"segment": "priority" if segment == "优先处理" else "all"},
        )
    except httpx.HTTPStatusError:
        st.info("证据复核暂不可用：请回到“综合分析”页，完成报告生成后再复核。")
        return
    stats = desk.get("stats") or {}
    st.caption(
        f"优先队列 {stats.get('high_risk', 0) + stats.get('high_impact', 0)} 条"
        f"（高危 {stats.get('high_risk', 0)} · 高影响 {stats.get('high_impact', 0)}）"
        f" · 全队列 {stats.get('actionable', 0)} 条 · 已排除 {stats.get('excluded', 0)} 条"
    )
    items = desk.get("items") or []
    if not items:
        st.success("没有需要复核的证据。自动存疑且被结论引用的证据会最先出现在这里。")
        return
    for item in items:
        _render_desk_item(project_id, item)


def render_analysis_report(project_id: str, workspace: dict) -> None:
    # Delegates to the unified incremental workspace (see render_analysis_workspace).
    render_analysis_workspace(project_id, workspace)


def render_wait(_project_id: str, _workspace: dict) -> None:
    st.status("后台研究正在进行，可以关闭页面后稍后再回来。", state="running")


def render_retry(project_id: str, _workspace: dict) -> None:
    if st.button("安全重试", type="primary", icon=":material/refresh:"):
        submit_action(project_id, {"type": "run"})
        st.rerun()


_ANALYSIS_STAGE_ORDER = (
    "index", "visuals", "problem", "method", "experiment",
    "critical", "overview", "auto_verify",
)
_PART_CHOICES = (
    ("problem", "问题与贡献"), ("method", "方法与机制"),
    ("experiment", "实验与结果"), ("critical", "局限与相关性"),
    ("overview", "综合概述"),
)
_PART_LABELS = dict(_PART_CHOICES)


def _board_chip(status: str, label: str, done=None, total=None) -> str:
    suffix = ""
    if total is not None and status == "running":
        suffix = f" {done}/{total}"
    elif total is not None and status == "completed":
        suffix = f" {total}"
    if status == "completed":
        return f":green[✓ {label}{suffix}]"
    if status == "running":
        return f":orange[⏳ {label}{suffix}]"
    if status == "failed":
        return f":red[✗ {label}{suffix}]"
    return f":gray[○ {label}{suffix}]"


def render_analysis_board(workspace: dict) -> None:
    """Per-paper stage board: exactly where each analysis is right now."""
    rows = workspace.get("analysis_board") or []
    if not rows:
        return
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row.get("paper_id") or "", []).append(row)
    for paper_id, paper_rows in groups.items():
        title = paper_rows[0].get("paper_title")
        if not title:
            title = "综合分析报告" if paper_id == "" else "论文"
        by_stage = {row["stage_key"]: row for row in paper_rows}
        ordered = [
            by_stage[key] for key in _ANALYSIS_STAGE_ORDER if key in by_stage
        ]
        ordered += [row for row in paper_rows if row not in ordered]
        chips = [
            _board_chip(
                row["status"], row.get("label") or row["stage_key"],
                row.get("done"), row.get("total"),
            )
            for row in ordered
        ]
        st.markdown(f"**{title}**")
        st.markdown("  ".join(chips))


def render_live_analysis_controls(project_id: str, workspace: dict) -> None:
    """Running-analysis view: live board + cooperative pause."""
    if workspace["user_stage"] in {"acquiring_selected", "analyzing_selected"}:
        render_analysis_board(workspace)
        if st.button(
            "暂停分析",
            icon=":material/pause:",
            key=f"pause-analysis-{project_id}",
            help="在当前图表或分析块结束后停下，已完成结果保留，之后可继续或局部重跑。",
        ) and submit_action(project_id, {"type": "pause_analysis"}):
            # Land on the analysis tab so the paused panel (continue /
            # partial re-analysis + research materials) is visible right
            # away instead of hiding behind a stale step selection.
            st.session_state[f"workflow-view-{project_id}"] = 4
            st.rerun()


def render_paused_analysis(project_id: str, workspace: dict) -> None:
    hint = workspace.get("budget_hint") or {}
    hitl_events = workspace.get("hitl_events") or []
    review_event = next(
        (event for event in hitl_events if event.get("type") == "evidence_review_gate"),
        None,
    )
    if review_event is not None:
        st.warning(
            "**合成前证据复核门**："
            + (review_event.get("reason") or "存在被引用且自动存疑的证据，综合报告生成前需要你决定。")
        )
        if st.button(
            "直接生成报告（跳过复核）",
            type="primary",
            icon=":material/skip_next:",
            key=f"review-continue-{project_id}",
        ) and submit_action(project_id, {"type": "review_gate_continue"}):
            st.rerun()
        st.caption(
            "也可以先在下方「查看研究资料」卡片确认/存疑/排除争议证据，"
            "再选择“先处理争议证据，再重新生成”。"
        )
        if st.button(
            "先处理争议证据，再重新生成",
            icon=":material/refresh:",
            key=f"review-regenerate-{project_id}",
        ) and submit_action(project_id, {"type": "review_gate_regenerate"}):
            st.rerun()
        return
    if hint.get("gate_enabled"):
        st.warning(
            "已达成本门槛，分析在安全边界自动暂停（已完成结果全部保留）："
            f"本轮 token {hint.get('tokens_total', 0):,} / 阈值 {hint.get('gate_tokens', 0):,}"
            f" · 视觉调用 {hint.get('vision_calls_total', 0)} / "
            f"{hint.get('gate_vision_calls', 0)}"
            f" · 已运行约 {hint.get('elapsed_minutes', 0)} / {hint.get('gate_minutes', 0)} 分钟。"
        )
    else:
        st.warning("分析已在安全边界暂停，已完成的结果均已保存。")
    render_analysis_board(workspace)
    st.caption(
        "中间产物（已解析 PDF、图表证据、缓存块、已完成论文）都会保留：可在下方"
        "「查看研究资料」查看已生成的证据；点「继续分析」会跳过已完成部分，不重复消耗 token。"
    )
    if st.button(
        "继续分析",
        type="primary",
        icon=":material/play_arrow:",
        key=f"resume-analysis-{project_id}",
        help=(
            "从停点续跑，已完成部分不会重复计算"
            if not hint.get("gate_enabled")
            else "放行剩余部分并从当前用量重新开始计量（成本门槛再次按新窗口计算）"
        ),
    ) and submit_action(project_id, {"type": "resume_analysis"}):
        st.rerun()
    render_partial_reanalysis(project_id, workspace)


def render_partial_reanalysis(project_id: str, workspace: dict) -> None:
    """Pick one paper block, add an extra requirement and re-run only that block."""
    tokens = workspace.get("selected_paper_tokens") or []
    if not tokens:
        return
    by_token = {
        paper["paper_token"]: paper["title"]
        for paper in (workspace.get("literature") or [])
    }
    labels = {token: by_token.get(token, "论文") for token in tokens}
    with st.expander("重新分析某一部分（可补充要求）", expanded=False):
        paper_token = st.selectbox(
            "选择论文", list(tokens), format_func=labels.get,
            key=f"part-paper-{project_id}",
        )
        part = st.selectbox(
            "选择部分", [key for key, _ in _PART_CHOICES],
            format_func=lambda key: _PART_LABELS[key],
            key=f"part-key-{project_id}",
        )
        instruction = st.text_area(
            "对该部分的补充要求（可选）",
            placeholder="例如：方法部分请补充伪代码步骤和输入输出格式说明",
            key=f"part-instruction-{project_id}",
        )
        submitted = st.button(
            "提交局部重析",
            type="primary",
            icon=":material/tune:",
            key=f"part-submit-{project_id}",
            help="只重跑该部分（其余部分复用缓存），完成后重新生成综合分析报告。",
        )
        if not submitted:
            return
        payload = {
            "type": "reanalyze_part", "paper_token": paper_token, "part": part,
        }
        if instruction.strip():
            payload["instruction"] = instruction.strip()
        if submit_action(project_id, payload):
            st.session_state[f"workflow-view-{project_id}"] = 4
            st.rerun()


ACTION_RENDERERS = {
    "wait": render_wait,
    "upload_documents": lambda project_id, workspace: render_documents(
        project_id, workspace["next_action"]
    ),
    "retry": render_retry,
}


@st.fragment(run_every=3)
def render_workspace(project_id: str) -> None:
    try:
        workspace = get_json(f"/projects/{project_id}/workspace")
    except httpx.HTTPError as exc:
        st.error(f"无法加载项目：{exc}")
        return
    selected_stage = step_header(workspace["user_stage"], project_id)
    st.markdown(f"## {workspace['name']}")
    if workspace["user_stage"] == "failed":
        st.error(f"**{workspace['status_label']}** — {workspace['status_detail']}")
        render_retry(project_id, workspace)
    else:
        st.info(f"**{workspace['status_label']}** — {workspace['status_detail']}")
    # Event-ized HITL (waiting_for_human): a waiting project holding an open
    # hitl_event surfaces why it is parked and what the human can do next.
    hitl_events = workspace.get("hitl_events") or []
    if workspace.get("waiting_for_human") and hitl_events:
        event = hitl_events[0]
        reason = f"——{event['reason']}" if event.get("reason") else ""
        st.warning(f"**待你决定**：{event['title']}{reason}")
    progress = workspace["progress"]
    st.caption(
        f"找到论文 {progress.get('found_papers', 0)} 篇 · "
        f"获得全文 {progress.get('full_text_papers', 0)} 篇 · "
        f"已分析页面 {progress.get('analyzed_pages', 0)} 页 · "
        f"已分析图表 {progress.get('analyzed_visuals', 0)} 个"
    )
    if any(progress.values()):
        st.caption(f"已完成 {progress['completed_items']} 项 · 正在处理 "
                   f"{progress['running_items']} 项 · 失败 {progress['failed_items']} 项")
    total_visuals = progress.get("total_visuals", 0)
    completed_visuals = progress.get("completed_visuals", 0)
    current_visual = progress.get("current_visual")
    if total_visuals and workspace["user_stage"] in {
        "acquiring_selected", "analyzing_selected"
    }:
        if current_visual:
            kind = "图" if current_visual.get("kind") == "figure" else "表"
            label = current_visual.get("label") or ""
            label_suffix = f"（{label}）" if label else ""
            progress_text = (
                f"正在分析第 {current_visual['page']} 页 · "
                f"{kind} {current_visual['kind_index']}/{current_visual['kind_total']}"
                f"{label_suffix} · 图表 {current_visual['index']}/{current_visual['total']}"
            )
        elif completed_visuals < total_visuals:
            progress_text = (
                f"正在分析第 {completed_visuals + 1}/{total_visuals} 个图表"
            )
        else:
            progress_text = progress.get("current_step") or "图表分析已完成"
        st.progress(completed_visuals / total_visuals, text=progress_text)
    hint = workspace.get("budget_hint")
    if hint and (hint.get("total_visuals") or hint.get("tokens_total")):
        parts = []
        if hint.get("total_visuals"):
            parts.append(
                f"已完成 {hint['completed_visuals']}/{hint['total_visuals']} 个图表视觉分析"
                f"（剩余 {hint['remaining_visuals']} 个，每个还可能产生一致性/自动复核调用）"
            )
        if hint.get("tokens_total"):
            parts.append(f"本轮 token {hint['tokens_total']:,}")
        if hint.get("vision_calls_total"):
            parts.append(f"视觉调用 {hint['vision_calls_total']:,}")
        hint_text = "预算：" + " · ".join(parts) + (
            f" · 已运行约 {hint['elapsed_minutes']} 分钟"
        )
        if hint.get("warned"):
            gate_note = (
                "；超过成本门槛将自动暂停在安全边界，等你决定继续或中止"
                if hint.get("gate_enabled") else "（仅提示，不自动中止）"
            )
            st.warning(
                f"{hint_text}。提醒阈值：图表 ≥ {hint['warning_vision_calls']} 个"
                f" 或时长 ≥ {hint['warning_minutes']} 分钟{gate_note}"
            )
        else:
            st.caption(hint_text)
    analysis_stage = workspace["user_stage"] in {
        "acquiring_selected", "analyzing_selected", "paused", "analysis_review", "failed",
    }
    has_analysis = bool(
        workspace.get("analysis_blocks") or workspace.get("analysis_papers")
        or workspace.get("analysis_report")
    )
    # Analysis-stage controls stay reachable on every step tab; the step-4 tab
    # additionally renders the full incremental workspace (blocks + PDF). We
    # avoid double-rendering the controls: for step 4 the workspace owns them.
    if workspace["user_stage"] in {"acquiring_selected", "analyzing_selected"} and selected_stage != 4:
        render_live_analysis_controls(project_id, workspace)
    elif workspace["user_stage"] == "paused" and selected_stage != 4:
        render_paused_analysis(project_id, workspace)
    if selected_stage == 1:
        render_project_setup(project_id, workspace)
    elif selected_stage == 2:
        render_search_stage(project_id, workspace)
        render_paper_selection(project_id, workspace)
    elif selected_stage == 3:
        render_paper_selection(project_id, workspace)
    else:
        if analysis_stage and has_analysis:
            render_analysis_workspace(project_id, workspace)
        elif workspace["user_stage"] == "paused":
            render_paused_analysis(project_id, workspace)
        elif workspace["user_stage"] != "failed":
            renderer = ACTION_RENDERERS.get(workspace["next_action"]["type"])
            if renderer:
                renderer(project_id, workspace)
        show_materials = st.toggle("查看研究资料", key=f"show-materials-{project_id}")
        if show_materials:
            render_research_materials(project_id, workspace)
    if st.toggle("显示高级诊断", key="show_diagnostics"):
        st.json(workspace["diagnostics"], expanded=False)


st.set_page_config(page_title="ResearchPilot", page_icon="📄", layout="wide")
st.title("ResearchPilot")
st.caption("本地优先、证据可追溯的多模态论文研究助手")
st.session_state.setdefault("project_id", None)
st.session_state.setdefault("creating_new", False)

try:
    health_data = get_json("/health")
    projects = get_json("/projects")
except httpx.HTTPError as exc:
    st.error(f"ResearchPilot API 暂不可用：{exc}")
    st.stop()

project_ids = [project["id"] for project in projects]
if (
    projects
    and not st.session_state.creating_new
    and st.session_state.project_id not in project_ids
):
    st.session_state.project_id = project_ids[0]

with st.sidebar:
    st.header("研究项目")
    if st.button("新建课题", icon=":material/add:"):
        st.session_state.project_id = None
        st.session_state.creating_new = True
        st.rerun()
    if projects:
        st.caption("课题列表")
        for project in projects:
            project_id = project["id"]
            is_current = (
                not st.session_state.creating_new
                and st.session_state.project_id == project_id
            )
            project_column, delete_column = st.columns(
                [5, 1], gap="small", vertical_alignment="center"
            )
            with project_column:
                if st.button(
                    project["name"],
                    key=f"open-project-{project_id}",
                    type="primary" if is_current else "secondary",
                    help=project["status_label"],
                    width="stretch",
                ):
                    st.session_state.project_id = project_id
                    st.session_state.creating_new = False
                    st.rerun()
            with delete_column:
                if st.button(
                    "×",
                    key=f"request-delete-{project_id}",
                    help=f"删除课题：{project['name']}",
                ):
                    remaining_ids = [value for value in project_ids if value != project_id]
                    confirm_project_deletion(project_id, project["name"], remaining_ids)

if st.session_state.project_id and not st.session_state.creating_new:
    render_workspace(st.session_state.project_id)
else:
    step_header("setup")
    render_setup(health_data)
