const STORAGE_PREFIX = "cooragent.workflowDiagram.v1";
const diagramRoot = document.getElementById("standaloneDiagram");
const pageTitle = document.getElementById("diagramPageTitle");
const pageMeta = document.getElementById("diagramPageMeta");
const downloadButton = document.getElementById("downloadDiagramPng");
const printButton = document.getElementById("printDiagram");

const getWorkflowId = () => new URLSearchParams(window.location.search).get("workflow_id") || "";

const getStoredDiagram = (workflowId) => {
  try {
    return JSON.parse(localStorage.getItem(`${STORAGE_PREFIX}:${workflowId}`) || "null");
  } catch (error) {
    console.warn("Failed to load standalone workflow diagram:", error);
    return null;
  }
};

const renderError = (message) => {
  diagramRoot.replaceChildren();
  const error = document.createElement("div");
  error.className = "workflow-diagram-error";
  error.textContent = message;
  diagramRoot.appendChild(error);
  downloadButton.disabled = true;
};

const loadDiagram = () => {
  const workflowId = getWorkflowId();
  const payload = workflowId ? getStoredDiagram(workflowId) : null;
  if (!payload || payload.workflowId !== workflowId || !payload.diagramHtml) {
    renderError("没有找到流程图数据，请返回 Workflows 页面并重新点击流程图。");
    return;
  }

  const parsed = new DOMParser().parseFromString(payload.diagramHtml, "text/html");
  const diagram = parsed.querySelector(".workflow-architecture");
  if (!diagram) {
    renderError("流程图数据格式无效，请返回 Workflows 页面重新打开。");
    return;
  }
  diagram.querySelectorAll("script, iframe, object, embed, link, style").forEach((element) => element.remove());
  [diagram, ...diagram.querySelectorAll("*")].forEach((element) => {
    Array.from(element.attributes).forEach((attribute) => {
      if (attribute.name.toLowerCase().startsWith("on")) element.removeAttribute(attribute.name);
    });
  });
  diagram.removeAttribute("role");
  diagram.removeAttribute("tabindex");
  diagram.removeAttribute("title");
  diagramRoot.replaceChildren(document.importNode(diagram, true));
  pageTitle.textContent = payload.title || "Workflow Diagram";
  pageMeta.textContent = payload.workflowId;
  document.title = `${payload.title || "Workflow Diagram"} - CoorAgent`;
};

const numberValue = (value, fallback = 0) => {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const roundedRectPath = (context, x, y, width, height, radius) => {
  const safeRadius = Math.max(0, Math.min(radius, width / 2, height / 2));
  context.beginPath();
  context.moveTo(x + safeRadius, y);
  context.lineTo(x + width - safeRadius, y);
  context.quadraticCurveTo(x + width, y, x + width, y + safeRadius);
  context.lineTo(x + width, y + height - safeRadius);
  context.quadraticCurveTo(x + width, y + height, x + width - safeRadius, y + height);
  context.lineTo(x + safeRadius, y + height);
  context.quadraticCurveTo(x, y + height, x, y + height - safeRadius);
  context.lineTo(x, y + safeRadius);
  context.quadraticCurveTo(x, y, x + safeRadius, y);
  context.closePath();
};

const drawElementBox = (context, element, rootRect) => {
  const rect = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  const x = rect.left - rootRect.left;
  const y = rect.top - rootRect.top;
  const radius = numberValue(style.borderTopLeftRadius);
  roundedRectPath(context, x, y, rect.width, rect.height, radius);
  if (style.backgroundColor !== "rgba(0, 0, 0, 0)") {
    context.fillStyle = style.backgroundColor;
    context.fill();
  }
  const borderWidth = numberValue(style.borderTopWidth);
  if (borderWidth > 0 && style.borderTopColor !== "rgba(0, 0, 0, 0)") {
    context.strokeStyle = style.borderTopColor;
    context.lineWidth = borderWidth;
    context.stroke();
  }
};

const drawDownArrow = (context, element, rootRect) => {
  const rect = element.getBoundingClientRect();
  const centerX = rect.left - rootRect.left + (rect.width / 2);
  const top = rect.top - rootRect.top;
  const tipY = rect.bottom - rootRect.top - 2;
  const headTop = tipY - 13;
  context.strokeStyle = "#394454";
  context.fillStyle = "#394454";
  context.lineWidth = 3;
  context.beginPath();
  context.moveTo(centerX, top);
  context.lineTo(centerX, headTop);
  context.stroke();
  context.beginPath();
  context.moveTo(centerX - 10, headTop);
  context.lineTo(centerX + 10, headTop);
  context.lineTo(centerX, tipY);
  context.closePath();
  context.fill();
};

const drawFlowArrow = (context, element, rootRect) => {
  const rect = element.getBoundingClientRect();
  const startX = rect.left - rootRect.left;
  const tipX = rect.right - rootRect.left;
  const centerY = rect.top - rootRect.top + (rect.height / 2);
  const headStart = tipX - 14;
  context.strokeStyle = "#394454";
  context.fillStyle = "#394454";
  context.lineWidth = 3;
  context.beginPath();
  context.moveTo(startX, centerY);
  context.lineTo(headStart, centerY);
  context.stroke();
  context.beginPath();
  context.moveTo(headStart, centerY - 10);
  context.lineTo(headStart, centerY + 10);
  context.lineTo(tipX, centerY);
  context.closePath();
  context.fill();
};

const drawConfirmation = (context, element, rootRect) => {
  const rect = element.getBoundingClientRect();
  const x = rect.left - rootRect.left;
  const y = rect.top - rootRect.top;
  const drawDiamond = (inset, color) => {
    context.beginPath();
    context.moveTo(x + (rect.width / 2), y + inset);
    context.lineTo(x + rect.width - inset, y + (rect.height / 2));
    context.lineTo(x + (rect.width / 2), y + rect.height - inset);
    context.lineTo(x + inset, y + (rect.height / 2));
    context.closePath();
    context.fillStyle = color;
    context.fill();
  };
  drawDiamond(0, "#42a988");
  drawDiamond(3, "#e9f8f2");
};

const getTextLines = (textNode) => {
  const lines = [];
  const text = textNode.nodeValue || "";
  for (let index = 0; index < text.length; index += 1) {
    const range = document.createRange();
    range.setStart(textNode, index);
    range.setEnd(textNode, index + 1);
    const rect = range.getBoundingClientRect();
    range.detach();
    if (!rect.width && !rect.height) continue;
    let line = lines.find((item) => Math.abs(item.top - rect.top) < 1.5);
    if (!line) {
      line = { top: rect.top, left: rect.left, text: "" };
      lines.push(line);
    }
    line.left = Math.min(line.left, rect.left);
    line.text += text[index];
  }
  return lines;
};

const drawDiagramText = (context, diagram, rootRect) => {
  const walker = document.createTreeWalker(diagram, NodeFilter.SHOW_TEXT);
  let textNode = walker.nextNode();
  while (textNode) {
    if (textNode.nodeValue?.trim()) {
      const parent = textNode.parentElement;
      const style = getComputedStyle(parent);
      if (style.display !== "none" && style.visibility !== "hidden") {
        context.fillStyle = style.color;
        context.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
        context.textBaseline = "top";
        getTextLines(textNode).forEach((line) => {
          context.fillText(line.text, line.left - rootRect.left, line.top - rootRect.top);
        });
      }
    }
    textNode = walker.nextNode();
  }
};

const renderDiagramCanvas = (diagram) => {
  const rootRect = diagram.getBoundingClientRect();
  const padding = 32;
  const exportWidth = Math.ceil(rootRect.width + (padding * 2));
  const exportHeight = Math.ceil(rootRect.height + (padding * 2));
  const scale = Math.min(2, 4096 / Math.max(exportWidth, exportHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.ceil(exportWidth * scale);
  canvas.height = Math.ceil(exportHeight * scale);
  const context = canvas.getContext("2d");
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.scale(scale, scale);
  context.translate(padding, padding);
  context.lineJoin = "round";

  const executionZone = diagram.querySelector(".workflow-architecture-execution-zone");
  if (executionZone) drawElementBox(context, executionZone, rootRect);
  diagram.querySelectorAll(".workflow-architecture-down-arrow").forEach((element) => {
    drawDownArrow(context, element, rootRect);
  });
  diagram.querySelectorAll(".workflow-architecture-flow-arrow").forEach((element) => {
    drawFlowArrow(context, element, rootRect);
  });
  diagram.querySelectorAll([
    ".workflow-architecture-system",
    ".workflow-architecture-agent",
    ".workflow-architecture-info-row",
    ".workflow-architecture-zone-title",
    ".workflow-architecture-end",
  ].join(",")).forEach((element) => drawElementBox(context, element, rootRect));
  const confirmation = diagram.querySelector(".workflow-architecture-confirm");
  if (confirmation) drawConfirmation(context, confirmation, rootRect);
  drawDiagramText(context, diagram, rootRect);
  return canvas;
};

const downloadDiagramPng = async () => {
  const diagram = diagramRoot.querySelector(".workflow-architecture");
  if (!diagram || downloadButton.disabled) return;
  const originalLabel = downloadButton.textContent;
  downloadButton.disabled = true;
  downloadButton.textContent = "生成中...";
  try {
    const canvas = renderDiagramCanvas(diagram);
    const pngBlob = await new Promise((resolve, reject) => {
      canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("Failed to encode PNG")), "image/png");
    });
    const downloadUrl = URL.createObjectURL(pngBlob);
    const link = document.createElement("a");
    const safeName = getWorkflowId().replace(/[^a-zA-Z0-9_-]+/g, "_") || "workflow";
    link.href = downloadUrl;
    link.download = `${safeName}_diagram.png`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
  } catch (error) {
    console.error("Failed to export workflow diagram:", error);
    window.alert("PNG 生成失败，请使用打印功能或浏览器截图保存。");
  } finally {
    downloadButton.disabled = false;
    downloadButton.textContent = originalLabel;
  }
};

downloadButton.addEventListener("click", downloadDiagramPng);
printButton.addEventListener("click", () => window.print());
loadDiagram();
