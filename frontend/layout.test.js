const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync(require("node:path").join(__dirname, "index.html"), "utf8");
const lines = html.split(/\r?\n/);

function functionLine(name) {
  const prefix = `function ${name}`;
  const matches = lines.filter((line) => line.startsWith(prefix));
  assert.equal(matches.length, 1, `${name} 应且只能定义一次`);
  return matches[0];
}

assert.equal((html.match(/function startNodeDrag\s*\(/g) || []).length, 1, "节点拖拽实现不能重复");
assert.equal((html.match(/function startPropertyDrag\s*\(/g) || []).length, 0, "已废弃的属性拖拽函数应完全删除");
assert.equal(html.includes("ghost.textContent=`迁移 ${prop.name}`"), false, "已废弃的属性拖拽迁移实现应删除");
assert.ok(html.includes('id="operations"'), "应提供 Outbox 运行监控页面");
assert.ok(html.includes("api('/actions/outbox/alerts?limit=100')"), "监控页应读取死信告警接口");
assert.ok(html.includes("/replay`"), "监控页应调用人工重放接口");
assert.ok(html.includes("原 Action 终态"), "人工重放界面应明确保留原 Action 历史语义");
assert.ok(html.includes('id="operationsBadge"'), "导航应提供活动告警红点");
assert.ok(html.includes("api('/operations/summary')"), "运行监控应读取主动告警摘要");
assert.ok(html.includes("api('/operations/circuits')"), "运行监控应展示外部依赖熔断器");
assert.ok(html.includes("operateOnAlert(item,'acknowledge')"), "告警应支持确认处理");
assert.ok(html.includes("operateOnAlert(item,'resolve')"), "告警应支持标记解决");
assert.ok(html.includes('id="workflows"'), "应提供个人信审流程编排页面");
assert.ok(html.includes("api('/workflows/personal-credit/summary')"), "流程页应读取流程摘要");
assert.ok(html.includes('/steps/${encodeURIComponent(actionName)}/execute'), "流程页应通过编排接口派发 Action");
assert.ok(html.includes('trigger_manual_review'), "流程时间线应包含条件人工复核步骤");
assert.ok(html.includes('不代表最终审批'), "流程页必须保留决策建议安全边界");

const layoutEffects = { updateCalls: 0, message: null };
const layoutContext = {
  console,
  graphPreview: { clientWidth: 1200 },
  ontologyMsg: {},
  selectedOntologyName: "Vehicle",
  show(_element, message, ok) {
    layoutEffects.message = { message, ok };
  },
  updatePreviewPositions() {
    layoutEffects.updateCalls += 1;
  },
  renderOntologyPreview() {
    throw new Error("已有预览时不应重新渲染本体");
  },
};
layoutContext.previewState = {
  svg: {
    attrs: {},
    style: {},
    setAttribute(name, value) {
      this.attrs[name] = value;
    },
  },
  nodes: Array.from({ length: 9 }, (_, index) => ({
    id: `Type${index + 1}`,
    width: 250,
    height: 173,
    x: 0,
    y: 0,
  })),
  edges: [],
  width: 760,
  height: 560,
  ontology: { version: "unchanged" },
};
vm.createContext(layoutContext);
vm.runInContext(functionLine("applyPreviewGridLayout"), layoutContext);
vm.runInContext(functionLine("resetPreviewLayout"), layoutContext);

layoutContext.applyPreviewGridLayout();
const initialPositions = layoutContext.previewState.nodes.map(({ x, y }) => ({ x, y }));
let overlaps = 0;
for (let left = 0; left < layoutContext.previewState.nodes.length; left += 1) {
  for (let right = left + 1; right < layoutContext.previewState.nodes.length; right += 1) {
    const a = layoutContext.previewState.nodes[left];
    const b = layoutContext.previewState.nodes[right];
    if (
      Math.abs(a.x - b.x) < (a.width + b.width) / 2
      && Math.abs(a.y - b.y) < (a.height + b.height) / 2
    ) overlaps += 1;
  }
}
assert.equal(overlaps, 0, "初始网格布局不应出现卡片重叠");
assert.ok(layoutContext.previewState.height > 560, "节点较多时画布应自动增高");

const ontologyReference = layoutContext.previewState.ontology;
layoutContext.previewState.nodes[0].x += 80;
layoutContext.previewState.nodes[0].y += 60;
layoutContext.resetPreviewLayout();
assert.deepEqual(
  layoutContext.previewState.nodes.map(({ x, y }) => ({ x, y })),
  initialPositions,
  "重置应恢复初始网格坐标",
);
assert.equal(layoutContext.previewState.ontology, ontologyReference, "重置不得替换或修改本体对象");
assert.equal(layoutContext.selectedOntologyName, "Vehicle", "重置不得清除当前选中本体");
assert.equal(layoutEffects.updateCalls, 1, "重置后应刷新一次节点和关系坐标");
assert.deepEqual(layoutEffects.message, { message: "本体节点已恢复为无重叠初始布局", ok: true });

const listeners = new Map();
const group = {
  captured: null,
  released: null,
  setPointerCapture(pointerId) { this.captured = pointerId; },
  hasPointerCapture(pointerId) { return this.captured === pointerId; },
  releasePointerCapture(pointerId) { this.released = pointerId; },
  addEventListener(name, listener) { listeners.set(name, listener); },
  removeEventListener(name) { listeners.delete(name); },
};
const dragNode = { x: 150, y: 100, width: 250, height: 173, group };
const dragEffects = { updateCalls: 0 };
const dragContext = {
  console,
  suppressOntologyClick: false,
  previewState: { width: 1200, height: 627 },
  updatePreviewPositions() { dragEffects.updateCalls += 1; },
};
vm.createContext(dragContext);
vm.runInContext(functionLine("startNodeDrag"), dragContext);
const svg = {
  createSVGPoint() {
    return {
      x: 0,
      y: 0,
      matrixTransform() { return { x: this.x, y: this.y }; },
    };
  },
  getScreenCTM() { return { inverse() { return {}; } }; },
};
dragContext.startNodeDrag({
  isPrimary: true,
  button: 0,
  pointerId: 7,
  clientX: 150,
  clientY: 100,
  preventDefault() {},
}, dragNode, svg);
listeners.get("pointermove")({ clientX: 360, clientY: 280 });
listeners.get("pointerup")({ pointerId: 7 });
assert.deepEqual({ x: dragNode.x, y: dragNode.y }, { x: 360, y: 280 }, "拖动应更新节点坐标");
assert.equal(dragEffects.updateCalls, 1, "拖动应刷新关系位置");
assert.equal(dragContext.suppressOntologyClick, true, "拖动完成后应抑制同一次手势产生的点击");
assert.equal(group.released, 7, "拖动结束应释放 pointer capture");
assert.equal(listeners.size, 0, "拖动结束应移除临时监听器");

console.log(JSON.stringify({
  valid: true,
  node_count: layoutContext.previewState.nodes.length,
  overlaps,
  canvas_height: layoutContext.previewState.height,
  drag_position: { x: dragNode.x, y: dragNode.y },
}));
