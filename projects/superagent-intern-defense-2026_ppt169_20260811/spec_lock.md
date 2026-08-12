<!-- ppt-master-schema: spec-lock/v1 -->
# Execution Lock

## canvas
- viewBox: 0 0 1280 720
- format: PPT 16:9

## communication
- primary_language: zh-CN
- audience: 实习课题答辩评审老师、带教老师与项目负责人
- objective: 通过重点同类产品对标、四模块机制、量化评测和现场演示，使评委确认课题覆盖任务书要求、理解 SuperAgent 的关键差异并认可其证据与边界。
- core_message: SuperAgent 用 TaskProfile、可信 TaskGraph、Memory/Skill 与 S-ABAC，把开放式模型能力收敛为可校验、可编排、可恢复、可治理的数字员工执行闭环。
- consumption_mode: balanced

## mode
- mode: pyramid

## visual_style
- visual_style: custom
- visual_style_references: swiss-minimal, data-journalism, editorial
- visual_style_behavior: swiss-minimal 提供严格栅格、大留白、锐利矩形和极少装饰；data-journalism 提供指标条、微型图表、来源行和高密度但可读的数据结构；editorial 提供章节编号、细规则与证据层级。延续参考 PDF 的蓝橙交叠圆弧作为跨页识别符，但不复用其品牌元素。

## colors
- background: #FFFFFF
- secondary_bg: #F3F7FB
- primary: #0B5CAD
- accent: #F28C28
- secondary_accent: #4CA6C6
- body_text: #1E2A36
- grid: #DCE5EF
- positive: #21A179
- negative: #D95C5C

## typography
- font_family: Microsoft YaHei, Arial, sans-serif
- title_family: Microsoft YaHei, Arial, sans-serif
- body_family: Microsoft YaHei, Arial, sans-serif
- data_family: Arial, Microsoft YaHei, sans-serif
- code_family: Consolas, Microsoft YaHei, monospace
- body: 24
- title: 42
- subtitle: 32
- annotation: 18
- chapter_title: 54
- chapter_number: 128
- data: 30
- code: 20
- footnote: 14

## icons
- library: tabler-outline
- stroke_width: 2
- inventory: tabler-outline/map-route, tabler-outline/network, tabler-outline/shield-lock, tabler-outline/tools, tabler-outline/brain, tabler-outline/database, tabler-outline/history, tabler-outline/chart-bar, tabler-outline/users-group, tabler-outline/presentation-analytics, tabler-outline/player-pause, tabler-outline/checks, tabler-outline/clipboard-check

## images
- p06: images/image_006.png | source=user | pattern=#P1-02 Side image with content field；完整截图与左侧指标条形成证据对照 | crop=no-crop
- p16: images/image_010.png | source=user | pattern=#P1-12 Framed figure with caption；横向完整截图置于四道闸门结构旁 | crop=no-crop

## page_rhythm
- P01: anchor
- P02: dense
- P03: breathing
- P04: dense
- P05: dense
- P06: dense
- P07: breathing
- P08: dense
- P09: dense
- P10: dense
- P11: breathing
- P12: dense
- P13: dense
- P14: breathing
- P15: dense
- P16: dense
- P17: dense
- P18: dense

## pptx_structure
- mode: flat

## forbidden
- `mask`, `<style>`, `class`, external CSS, `<foreignObject>`, `textPath`, `@font-face`, `<animate*>`, `<set>`, `<script>` / event attributes, `<iframe>`
- HTML named entities in text; write typography as raw Unicode and escape XML reserved characters
