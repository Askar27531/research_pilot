# P7 Artifact 与 UI 验收报告

## 交付

- migration v4 与 Artifact Repository：项目隔离、同名递增版本、SHA-256、相对路径。
- Markdown、UTF-8 BOM CSV、Mermaid 三类生成器。
- Artifact MCP：`create_markdown`、`create_csv`、`create_mermaid`。
- FastAPI：生成、列表、校验后下载。
- Streamlit：New Research、Progress/Trace、Evidence、Experiment approval、Artifacts preview/download。

## 安全与一致性

- Artifact 名称只允许安全字符，文件始终写入项目 Workspace。
- 下载前重新计算 SHA-256；文件被修改时拒绝返回。
- CSV 每行列数必须一致。
- Mermaid 必须使用受支持声明，并拒绝 script、javascript、click、init 指令。
- UI 只调用 FastAPI，不导入数据库或 Repository。

## 启动

```powershell
python -m uvicorn app.main:app --reload
streamlit run ui/app.py
```
