"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const script = fs.readFileSync(path.join(__dirname, "credit-review.js"), "utf8");
const start = script.indexOf("async function loadWorkflowContext(");
const end = script.indexOf("async function connectBackend(", start);
assert.ok(start >= 0 && end > start);

function harness() {
  const pending = new Map(), events = [], errors = [];
  const state = {workflows: ["A", "B"].map(id => ({workflow_id: id, application_id: "APP-" + id, ontology_version: "1.1.0"}))};
  const context = {
    state, api: url => new Promise((resolve, reject) => pending.set(url, {resolve, reject})),
    renderContext() {}, resetConversationContext() {}, ensureConversationApplication() {}, newTask() {},
    addEvent: (...args) => events.push(args), toast: (...args) => errors.push(args),
  };
  vm.runInNewContext(script.slice(start, end), context);
  const detailUrl = id => "/workflows/personal-credit/" + id;
  const readyUrl = id => "/agents/personal-credit/APP-" + id + "/readiness?ontology_version=1.1.0";
  const flush = () => new Promise(resolve => setImmediate(resolve));
  async function complete(id) {
    pending.get(detailUrl(id)).resolve({workflow: {workflow_id: id}});
    await flush();
    pending.get(readyUrl(id)).resolve({application_id: "APP-" + id});
    await flush();
  }
  function assertCurrentB() {
    assert.equal(state.currentWorkflow.workflow_id, "B");
    assert.equal(state.workflowDetail.workflow.workflow_id, "B");
    assert.equal(state.readiness.application_id, "APP-B");
    assert.deepEqual(events.map(event => event[1]), ["APP-B"]);
    assert.deepEqual(errors, []);
  }
  return {state, pending, context, detailUrl, readyUrl, flush, complete, assertCurrentB};
}

(async () => {
  {
    const h = harness(), first = h.context.loadWorkflowContext("A");
    const second = h.context.loadWorkflowContext("B");
    await h.complete("B");
    h.pending.get(h.detailUrl("A")).resolve({workflow: {workflow_id: "A"}});
    await Promise.all([first, second]);
    assert.equal(h.pending.has(h.readyUrl("A")), false, "过期详情不得继续加载 A 的证据");
    h.assertCurrentB();
  }
  {
    const h = harness(), first = h.context.loadWorkflowContext("A");
    h.pending.get(h.detailUrl("A")).resolve({workflow: {workflow_id: "A"}});
    await h.flush();
    const second = h.context.loadWorkflowContext("B");
    await h.complete("B");
    h.pending.get(h.readyUrl("A")).resolve({application_id: "APP-A"});
    await Promise.all([first, second]);
    h.assertCurrentB();
  }
  {
    const h = harness(), first = h.context.loadWorkflowContext("A");
    const second = h.context.loadWorkflowContext("B");
    await h.complete("B");
    h.pending.get(h.detailUrl("A")).reject(new Error("A 已失效"));
    await Promise.all([first, second]);
    h.assertCurrentB();
  }
  console.log("credit-review context: 3 race regressions passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
