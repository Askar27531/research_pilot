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
def get_json(path: str, paper: str | None = None):
    params = {"paper": paper} if paper else None
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
              "analyzing_selected": 4, "analysis_review": 4,
              "direction_review": 4, "researching": 4, "experiment_review": 4,
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
        if metadata.get("abstract"):
            st.write(metadata["abstract"])
        summary = paper.get("summary")
        if summary:
            for field, title in (("method", "方法"), ("contributions", "贡献"),
                                 ("limitations", "局限")):
                values = summary.get(field, [])
                if values:
                    st.markdown(f"**{title}**")
                    for value in values:
                        st.write(f"- {value['value']}")
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
    if workspace.get("output_freshness") == "stale" and st.button(
        "根据复核更新研究结果", type="primary", icon=":material/refresh:"
    ):
        submit_action(project_id, {"type": "refresh_results"})
        st.rerun()


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
def render_paper_selection(project_id: str, workspace: dict) -> None:
    if not workspace.get("search_plan"):
        st.info("请先前往“检索文献”完成一次检索。")
        return

    papers = workspace.get("literature") or []
    st.subheader(f"选择论文（当前结果 {len(papers)} 篇）")
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
        with st.expander(paper["title"]):
            st.caption("、".join(paper.get("authors", [])))
            st.write(paper.get("abstract") or "暂无摘要")
            if paper.get("reason"):
                st.caption(f"相关性说明：{paper['reason']}")
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


def _render_pdf_reader(report: dict) -> None:
    papers = [paper for paper in report.get("papers", []) if paper.get("pdf_url")]
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


def render_analysis_report(_project_id: str, workspace: dict) -> None:
    report = workspace.get("analysis_report") or {}
    analysis_column, pdf_column = st.columns([3, 2], gap="medium")
    with analysis_column:
        _render_report_content(report)
    with pdf_column:
        _render_pdf_reader(report)
    if st.button(
        "基于现有证据重新生成详细分析",
        icon=":material/refresh:",
        help="保留已解析的 PDF、图表和证据，只重新生成更完整的分析报告。",
    ) and submit_action(_project_id, {"type": "reanalyze_selected"}):
        st.rerun()


def render_revision(project_id: str, target: str, label: str, placeholder: str) -> None:
    state_key = f"{target}_preview"
    with st.form(f"{target}_revision"):
        instruction = st.text_area(label, placeholder=placeholder)
        submitted = st.form_submit_button("生成调整预览")
    if submitted:
        result = submit_action(project_id, {
            "type": f"{target}_revision_preview", "instruction": instruction})
        if result:
            st.session_state[state_key] = result["preview"]
    preview = st.session_state.get(state_key)
    if preview:
        st.info(preview["summary"])
        if st.button("确认应用调整", type="primary", key=f"apply_{target}_revision"):
            submit_action(project_id, {
                "type": f"{target}_revision_apply",
                "preview_token": preview["preview_token"],
            })
            st.session_state.pop(state_key, None)
            st.rerun()


def render_direction(project_id: str, direction: dict) -> None:
    st.subheader("审阅研究方向")
    combinations = (direction or {}).get("combinations", [])[:3]
    for item in combinations:
        with st.container(border=True):
            st.markdown(f"### {item['title']}")
            st.write(item["target_challenge"])
            for step in item.get("integration_design", []):
                st.write(f"- {step}")
            st.caption("主要风险：" + "；".join(item.get("assumptions", [])))
    with st.container(horizontal=True):
        if st.button("采用这个方向", type="primary", icon=":material/check:"):
            submit_action(project_id, {"type": "direction_decision", "decision": "accept"})
            st.rerun()
        if st.button("暂不采用", icon=":material/close:"):
            submit_action(project_id, {"type": "direction_decision", "decision": "reject"})
            st.rerun()
    render_revision(project_id, "direction", "请帮我调整", "描述希望如何调整研究方向")


def render_plan(project_id: str, proposal: dict) -> None:
    st.subheader("确认研究方案")
    st.info("ResearchPilot 只生成研究和实验计划，不会执行实验或生成训练任务。")
    for experiment in (proposal or {}).get("experiments", []):
        with st.container(border=True):
            st.markdown(f"### {experiment['title']}")
            st.markdown(f"**Baseline**：{experiment['baseline']}")
            st.markdown(f"**计划改动**：{experiment['modification']}")
            st.write("指标：" + "、".join(experiment.get("metrics", [])))
            st.success("成功判据：" + experiment["success_criterion"])
            st.warning("失败判据：" + experiment["failure_criterion"])
    with st.container(horizontal=True):
        if st.button("确认方案并生成材料", type="primary", icon=":material/check:"):
            submit_action(project_id, {"type": "plan_decision", "decision": "accept"})
            st.rerun()
        if st.button("暂不采用方案", icon=":material/close:"):
            submit_action(project_id, {"type": "plan_decision", "decision": "reject"})
            st.rerun()
    render_revision(project_id, "plan", "调整方案", "描述希望修改的假设、指标或消融")


def render_completed(project_id: str, workspace: dict) -> None:
    st.success(workspace["status_detail"])
    for resource in workspace.get("resources", []):
        with st.container(border=True):
            st.write(f"**{resource['name']}** · v{resource['version']}")
            st.link_button("下载", f"{API}{resource['url']}", icon=":material/download:")


def render_wait(_project_id: str, _workspace: dict) -> None:
    st.status("后台研究正在进行，可以关闭页面后稍后再回来。", state="running")


def render_retry(project_id: str, _workspace: dict) -> None:
    if st.button("安全重试", type="primary", icon=":material/refresh:"):
        submit_action(project_id, {"type": "run"})
        st.rerun()


ACTION_RENDERERS = {
    "wait": render_wait,
    "upload_documents": lambda project_id, workspace: render_documents(
        project_id, workspace["next_action"]
    ),
    "review_direction": lambda project_id, workspace: render_direction(
        project_id, workspace["direction"]
    ),
    "review_experiment": lambda project_id, workspace: render_plan(
        project_id, workspace["experiment"]
    ),
    "download": render_completed,
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
    if total_visuals and workspace["user_stage"] in {
        "acquiring_selected", "analyzing_selected"
    }:
        if completed_visuals < total_visuals:
            progress_text = (
                f"正在分析第 {completed_visuals + 1}/{total_visuals} 个图表"
            )
        else:
            progress_text = progress.get("current_step") or "图表分析已完成"
        st.progress(completed_visuals / total_visuals, text=progress_text)
    if selected_stage == 1:
        render_project_setup(project_id, workspace)
    elif selected_stage == 2:
        render_search_stage(project_id, workspace)
        render_paper_selection(project_id, workspace)
    elif selected_stage == 3:
        render_paper_selection(project_id, workspace)
    else:
        if workspace.get("analysis_report"):
            render_analysis_report(project_id, workspace)
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
