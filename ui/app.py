import os

import httpx
import streamlit as st

API = os.getenv("RESEARCHPILOT_API_URL", "http://127.0.0.1:8000").rstrip("/")


def request(method: str, path: str, **kwargs):
    try:
        response = httpx.request(method, f"{API}{path}", timeout=30, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None
    except httpx.HTTPError as exc:
        st.error(f"API request failed: {exc}")
        return None


st.set_page_config(page_title="ResearchPilot", layout="wide")
st.title("ResearchPilot")
new_tab, progress_tab, evidence_tab, experiment_tab, artifact_tab = st.tabs(
    ["New Research", "Progress", "Evidence", "Experiment", "Artifacts"]
)

with new_tab:
    name = st.text_input("Project name")
    question = st.text_area("Research question")
    if st.button("Create project") and name and question:
        result = request(
            "POST",
            "/projects",
            json={"name": name, "request": {"research_question": question}},
        )
        if result:
            st.success(result["id"])

with progress_tab:
    project_id = st.text_input("Project ID", key="progress_project")
    event_filter = st.text_input("Event type filter", key="trace_event_filter")
    success_filter = st.selectbox("Success filter", ["all", "true", "false"])
    if st.button("Refresh", key="refresh_progress") and project_id:
        st.json(request("GET", f"/projects/{project_id}"))
        trace_metrics = request("GET", f"/projects/{project_id}/trace/metrics") or {}
        progress_metrics = request("GET", f"/projects/{project_id}/progress/metrics") or {}
        columns = st.columns(4)
        columns[0].metric("Success rate", f"{trace_metrics.get('success_rate', 0):.1%}")
        columns[1].metric(
            "Average latency", f"{trace_metrics.get('average_latency_ms') or 0:.0f} ms"
        )
        columns[2].metric("Recoveries", trace_metrics.get("recovery_count", 0))
        columns[3].metric("Completed items", progress_metrics.get("completed_items", 0))
        params = {}
        if event_filter:
            params["event_type"] = event_filter
        if success_filter != "all":
            params["success"] = success_filter
        st.subheader("Trace timeline")
        st.dataframe(
            request("GET", f"/projects/{project_id}/trace", params=params) or [],
            use_container_width=True,
        )
        st.subheader("Item progress")
        st.dataframe(
            request("GET", f"/projects/{project_id}/progress") or [],
            use_container_width=True,
        )

with evidence_tab:
    project_id = st.text_input("Project ID", key="evidence_project")
    paper_id = st.text_input("Paper ID")
    if st.button("Load evidence") and project_id and paper_id:
        st.json(request("GET", f"/projects/{project_id}/evidence", params={"paper_id": paper_id}))

with experiment_tab:
    project_id = st.text_input("Project ID", key="experiment_project")
    if st.button("Load proposal") and project_id:
        st.json(request("GET", f"/projects/{project_id}/experiment-proposal"))
    action = st.selectbox("Decision", ["accept", "modify", "reject"])
    version = st.number_input("Proposal version", min_value=1, value=1)
    feedback = st.text_area("Feedback")
    if st.button("Submit decision") and project_id:
        st.json(
            request(
                "POST",
                f"/projects/{project_id}/experiment-proposal/decision",
                json={"action": action, "version": version, "feedback": feedback or None},
            )
        )

with artifact_tab:
    project_id = st.text_input("Project ID", key="artifact_project")
    if st.button("Load artifacts") and project_id:
        artifacts = request("GET", f"/projects/{project_id}/artifacts") or []
        for artifact in artifacts:
            st.write(f"{artifact['name']} v{artifact['version']} ({artifact['artifact_type']})")
            if st.button("Preview", key=f"preview-{artifact['artifact_id']}"):
                preview = httpx.get(
                    f"{API}/projects/{project_id}/artifacts/{artifact['artifact_id']}/download",
                    timeout=30,
                )
                st.code(preview.text)
            st.link_button(
                "Download",
                f"{API}/projects/{project_id}/artifacts/{artifact['artifact_id']}/download",
            )
