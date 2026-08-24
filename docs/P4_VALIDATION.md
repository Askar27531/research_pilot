# P4 PDF 文档工作区验收报告

## 验收环境

- Parser：PyMuPDF 1.28.2
- 页面渲染：72 DPI（生产默认 144 DPI）
- 验收日期：2026-08-09
- 自动化语料：双栏、跨页、无嵌入图、矢量图、图像型页面五类合成 PDF
- 真实语料：5 篇公开 arXiv 论文，下载文件保存在被 Git 忽略的 `data/p4_validation_sources/`

## 真实开放论文结果

| 论文 | 页数 | 有文本页 | 截图 | Sections | Raster Figures | Tables | Warning |
|---|---:|---:|---:|---:|---:|---:|---:|
| [Attention Is All You Need](https://arxiv.org/abs/1706.03762) | 15 | 15 | 15 | 7 | 3 | 5 | 0 |
| [Generative Adversarial Nets](https://arxiv.org/abs/1406.2661) | 9 | 9 | 9 | 8 | 4 | 2 | 0 |
| [Deep Residual Learning for Image Recognition](https://arxiv.org/abs/1512.03385) | 12 | 12 | 12 | 15 | 0 | 17 | 0 |
| [U-Net](https://arxiv.org/abs/1505.04597) | 8 | 8 | 8 | 9 | 10 | 2 | 0 |
| [An Image is Worth 16x16 Words](https://arxiv.org/abs/2010.11929) | 22 | 22 | 22 | 5 | 39 | 13 | 0 |

聚合结果：66/66 页提取文本，66/66 页生成截图，0 个页面 warning。Figure 数量只统计 PDF 内嵌 raster image，不代表人工观察到的全部图。

## 已验证行为

- 导入顺序为大小、扩展名/PDF 魔数、SHA-256、document ID、复制、原子更新 manifest。
- 同一项目重复导入相同 SHA-256 不产生重复文档。
- 绝对路径、`..`、未知 project/document 和超大文件被拒绝。
- 页面、截图、结构、Figure 和 Table candidate 都只暴露项目相对路径。
- 单页渲染故障写入该页 error/warning，其余页面继续解析。
- 五个 Document MCP tool 均通过真实 MCP Client 调用。

## 已知边界

- 当前没有 OCR。扫描页能够渲染截图，但文本可能为空。
- 当前单独提取 raster image；矢量图仍保留在页面截图中，但不会生成独立 Figure artifact。ResNet 验收样例体现了这一限制。
- Section、caption 和 Table 均为启发式候选，不是人工标注 Gold Set；复杂公式、无编号 caption 或非常规字号可能产生漏检/误检。
- 第一版不处理加密 PDF、损坏交叉引用的深度修复，也不支持 DOCX/PPTX/XLSX。

这些边界不阻断 P4 的页面工作区闭环；OCR、矢量区域检测和人工标注 precision/recall 应作为后续增强，而不能在 P5 中伪装成已可靠提取的 Evidence。
