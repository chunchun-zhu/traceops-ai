"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const labels = {needs_info:"待补充信息",checking:"检查进行中",awaiting_confirmation:"待人工确认",handoff:"待人工接管",resolved:"人工已确认",handed_off:"已交接"};
const resultLabels = {normal:"正常",abnormal:"异常",unknown:"无法核验"};
let currentSession = null, devices = [], busy = false;
function message(text, error=false) { $("message").textContent=text;$("message").className="message"+(error?" error":""); }
async function api(path, method="GET", body) {
  const headers = {};
  const token=sessionStorage.getItem("suzhang-token");if(token)headers.Authorization="Bearer "+token;
  if(body && !(body instanceof FormData)) headers["Content-Type"]="application/json";
  const response=await fetch("/api"+path,{method,headers,body:body instanceof FormData?body:body?JSON.stringify(body):undefined});
  const data=await response.json();
  if(!response.ok) throw new Error(typeof data.detail==="string"?data.detail:JSON.stringify(data.detail));
  return data;
}
async function action(fn) {
  if(busy)return;busy=true;document.querySelectorAll("button").forEach(b=>b.disabled=true);
  $("message").classList.add("hidden");
  try { await fn(); } catch(error) {message(error.message,true);}
  finally {busy=false;document.querySelectorAll("button").forEach(b=>b.disabled=false);}
}
function time(value) {return new Date(value).toLocaleString("zh-CN",{hour12:false});}
function selectView(view) {
  document.querySelectorAll(".view").forEach(el=>el.classList.toggle("hidden",el.id!==view));
  document.querySelectorAll(".nav").forEach(el=>el.classList.toggle("active",el.dataset.view===view));
  $("page-title").textContent={workbench:"排障工作台",knowledge:"设备知识库",assets:"供气关系与测点"}[view];
}
function evidenceHTML(items) {
  if(!items.length)return '<p class="muted">没有找到适用依据。</p>';
  return items.map(c=>`<article class="evidence-item"><h3>${esc(c.title)}</h3><div class="evidence-meta">${esc(c.model)} / v${esc(c.version)} / 第 ${c.page} 页<br>${esc(c.section)} · 字符 ${c.start}–${c.end}</div><p>${esc(c.text.slice(0,260))}${c.text.length>260?"…":""}</p>${c.kind==="work_order"?`<p>初步判断：${esc(c.preliminary_cause||"未记录")}<br>最终确认：${esc(c.confirmed_cause||"未记录")}</p>`:""}<button class="source-btn" data-doc="${esc(c.document_id)}" data-page="${c.page}">核对来源原文 ↗</button></article>`).join("");
}
async function refreshHistory() {
  const sessions=await api("/sessions");
  $("history").innerHTML=sessions.length?sessions.map(s=>`<button class="history-item" data-session="${esc(s.id)}"><strong>${esc(s.symptom.slice(0,30))}</strong><small>${esc(labels[s.status])} · ${esc(time(s.updated_at))}</small></button>`).join(""):'<p class="muted">暂无排障记录</p>';
  $("history-select").innerHTML='<option value="">打开排障记录</option>'+sessions.map(s=>`<option value="${esc(s.id)}">${esc(labels[s.status])} · ${esc(s.symptom.slice(0,24))}</option>`).join("");
  $("history-select").value=currentSession?.id||"";
}
function renderSession(s) {
  currentSession=s;localStorage.setItem("suzhang-session",s.id);
  $("history-select").value=s.id;
  let content=`<div class="status-line"><span class="status">${esc(labels[s.status])}</span><span class="label">CASE ${s.id.slice(0,8).toUpperCase()} / REV ${s.revision}</span></div><p class="session-summary">${esc(s.summary)}</p>`;
  if(s.status==="needs_info")content+=`<div class="check"><h3>补充设备信息</h3><p>${esc(s.question)}</p><button class="primary" id="clarify">使用上方选中的设备继续</button></div>`;
  if(s.current_check)content+=`<div class="check"><div class="eyebrow">当前检查 / ${esc(s.current_check.id)}</div><h3>${esc(s.current_check.title)}</h3><p>${esc(s.current_check.instruction)}</p><p class="evidence-meta">核验节点：${esc((s.current_check.target_ids||[]).join("、"))}</p><label for="feedback-note">现场记录 / 读数与时间</label><textarea id="feedback-note" rows="2" maxlength="2000" placeholder="记录测点、读数、采集时间及核验依据"></textarea><div class="feedback-actions"><button data-result="normal">正常，继续</button><button data-result="abnormal">异常，待确认</button><button data-result="unknown">无法核验，转人工</button></div></div>`;
  if(s.candidates.length)content+=`<h3>候选原因</h3>${s.candidates.map(c=>`<div class="candidate"><span>${esc(c.name)}</span><em>${esc({pending:"尚未核验",not_supported:"本次反馈不支持",supported:"检查支持候选",unverified:"无法核验"}[c.state])}</em></div>`).join("")}`;
  if(s.model_explanation)content+=`<details open><summary>模型辅助说明 · 需人工核对</summary><p class="muted">${esc(s.model_explanation.summary)}</p><p class="evidence-meta">引用：${esc(s.model_explanation.evidence_ids.join(", "))}</p></details>`;
  if(!["resolved","handed_off"].includes(s.status)) content+=`<details ${["awaiting_confirmation","handoff"].includes(s.status)?"open":""}><summary>人工确认与交接</summary><label>记录人<input id="operator" maxlength="100" placeholder="姓名 / 班组"></label><label>确认结论 / 交接说明<textarea id="confirmation-note" rows="2" maxlength="2000" placeholder="填写独立核验依据、结论或下一班组需要继续检查的事项"></textarea></label><div class="feedback-actions">${s.status==="awaiting_confirmation"?'<button id="confirm">记录人工确认</button>':""}<button id="handoff">记录人工接管</button></div></details>`;
  if(s.confirmation)content+=`<details open><summary>人工处理记录</summary><p class="muted">${esc(s.confirmation.operator)} · ${esc(time(s.confirmation.at))}<br>${esc(s.confirmation.note)}</p></details>`;
  if(s.checks.length)content+=`<details open><summary>已完成检查 · ${s.checks.length} 项</summary><ol class="timeline">${s.checks.map(c=>`<li>${esc(c.title)} / ${esc(resultLabels[c.result])}<br>${esc(c.note||"未填写现场记录")} · ${esc(time(c.at))}</li>`).join("")}</ol></details>`;
  content+=`<details><summary>流程轨迹 · ${esc(s.skill.id)}@${esc(s.skill.version)}</summary><ol class="timeline">${s.trace.map(t=>`<li>${esc(t.node)} · ${esc(t.detail)}<br>${esc(time(t.at))}</li>`).join("")}</ol></details><div class="form-bottom"><span class="evidence-meta">${esc(s.mode)} / ${esc(s.retrieval_route)}</span><button id="export" class="outline">导出交接记录 JSON</button></div>`;
  $("session-panel").innerHTML=content;
  $("evidence").innerHTML=s.warnings.map(w=>`<p class="warning">${esc(w)}</p>`).join("")+evidenceHTML(s.evidence);
  $("paths").innerHTML=s.topology.length?s.topology.map(t=>`<div class="path"><strong>${esc(t.node.name)} / ${esc(t.node.id)}</strong>${t.paths.map(p=>`<span>${esc(p.join(" → "))}</span>`).join("")}<span>潜在影响：${esc(t.affected_devices.map(d=>d.name).join("、"))}</span></div>`).join(""):'<p class="muted">暂无连接路径</p>';
}
async function loadSession(id) {renderSession(await api("/sessions/"+encodeURIComponent(id)));selectView("workbench");}
function selectedDevices(){return Array.from(document.querySelectorAll("#device-picker input:checked")).map(el=>el.value);}
async function refreshDevices(){devices=await api("/devices");$("device-picker").innerHTML=devices.filter(d=>d.kind==="device").map(d=>`<label class="device-chip"><input type="checkbox" value="${esc(d.id)}" ${["PRESS-01","ASSY-01"].includes(d.id)?"checked":""}>${esc(d.name)}</label>`).join("");}
async function refreshDocuments(){const docs=await api("/documents");$("documents").innerHTML=`<h3>已入库资料 · ${docs.length} 份</h3>`+docs.map(d=>`<div class="doc-row"><button class="source-btn" data-doc="${esc(d.id)}" data-page="1">${esc(d.title)}</button><small>${esc(d.model)} / v${esc(d.version)} / ${d.chunk_count} 个切片</small></div>`).join("");}
async function refreshAssets(){
  const graph=await api("/topology");const names=Object.fromEntries(graph.nodes.map(n=>[n.id,n.name]));
  $("topology").innerHTML=graph.edges.map(e=>`<div class="topology-edge"><strong>${esc(names[e.source])}</strong><span>→</span><strong>${esc(names[e.target])}</strong><span>${esc(e.source)} → ${esc(e.target)}</span></div>`).join("")||'<p class="muted">尚未导入连接关系</p>';
  const points=await api("/measurements");$("measurements").innerHTML='<div class="table-scroll"><table><thead><tr><th>节点</th><th>压力 / MPa</th><th>采集时间</th><th>来源</th></tr></thead><tbody>'+points.map(p=>`<tr><td>${esc(p.node_id)}</td><td>${esc(p.value)}</td><td>${esc(time(p.observed_at))}</td><td>${esc(p.is_demo?"模拟数据":p.source)}</td></tr>`).join("")+'</tbody></table></div>';
}
document.querySelectorAll(".nav").forEach(b=>b.addEventListener("click",()=>action(async()=>{selectView(b.dataset.view);if(b.dataset.view==="knowledge")await refreshDocuments();if(b.dataset.view==="assets")await refreshAssets();})));
$("settings").addEventListener("click",()=>action(async()=>{const token=prompt("输入服务端配置的访问令牌。留空表示本地演示无令牌。令牌只保存在当前浏览器会话。");if(token===null)return;sessionStorage.setItem("suzhang-token",token);await refreshDevices();await refreshHistory();message("访问设置已更新");}));
$("refresh-history").addEventListener("click",()=>action(refreshHistory));
$("history-select").addEventListener("change",e=>{if(e.target.value)action(()=>loadSession(e.target.value));});
$("history").addEventListener("click",e=>{const b=e.target.closest("[data-session]");if(b)action(()=>loadSession(b.dataset.session));});
$("start-form").addEventListener("submit",e=>{e.preventDefault();action(async()=>{renderSession(await api("/sessions","POST",{device_ids:selectedDevices(),symptom:$("symptom").value}));await refreshHistory();});});
$("session-panel").addEventListener("click",e=>action(async()=>{
  const b=e.target.closest("button");if(!b||!currentSession)return;
  const s=currentSession;const base="/sessions/"+s.id;let next;
  if(b.dataset.result)next=await api(base+"/feedback","POST",{check_id:s.current_check.id,result:b.dataset.result,note:$("feedback-note").value,expected_revision:s.revision});
  else if(b.id==="clarify")next=await api(base+"/clarify","POST",{device_ids:selectedDevices(),expected_revision:s.revision});
  else if(b.id==="confirm"||b.id==="handoff")next=await api(base+"/confirm","POST",{decision:b.id==="confirm"?"confirmed":"handoff",operator:$("operator").value,note:$("confirmation-note").value,expected_revision:s.revision});
  else if(b.id==="export") {const data=await api(base+"/export");const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));const link=document.createElement("a");link.href=url;link.download="suzhang-"+s.id.slice(0,8)+".json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message("交接记录已导出");}
  if(next){renderSession(next);await refreshHistory();}
}));
document.addEventListener("click",e=>{const b=e.target.closest("[data-doc]");if(b)action(async()=>{const doc=await api("/documents/"+encodeURIComponent(b.dataset.doc));$("source-title").textContent=doc.title;$("source-content").innerHTML=`<p class="muted">${esc(doc.model)} / v${esc(doc.version)} · 引用页 ${esc(b.dataset.page)}</p>`+doc.pages.map((p,i)=>`<h3>第 ${i+1} 页</h3><pre>${esc(p)}</pre>`).join("");$("source-dialog").showModal();});});
$("close-source").addEventListener("click",()=>$("source-dialog").close());
$("search-form").addEventListener("submit",e=>{e.preventDefault();action(async()=>{const result=await api("/search","POST",{query:$("query").value,model:$("search-model").value||null,version:$("search-version").value||null,limit:8});$("search-results").innerHTML=`<p class="evidence-meta">路径 ${esc(result.route)} / 范围 ${result.total_candidates} 个切片</p>`+result.warnings.map(w=>`<p class="warning">${esc(w)}</p>`).join("")+evidenceHTML(result.items);});});
$("upload-form").addEventListener("submit",e=>{e.preventDefault();action(async()=>{const form=new FormData();form.append("file",$("upload-file").files[0]);form.append("model",$("upload-model").value);form.append("version",$("upload-version").value);const doc=await api("/documents/upload","POST",form);message(doc.duplicate?"相同资料已存在，未重复入库":`导入完成：${doc.chunk_count} 个知识切片`);await refreshDocuments();});});
$("topology-form").addEventListener("submit",e=>{e.preventDefault();action(async()=>{const data=JSON.parse(await $("topology-file").files[0].text());await api("/topology/import","POST",data);await refreshDevices();await refreshAssets();message("设备台账与连接关系已导入");});});
$("measurement-form").addEventListener("submit",e=>{e.preventDefault();action(async()=>{await api("/measurements","POST",{node_id:$("measurement-node").value,value:Number($("measurement-value").value),observed_at:new Date().toISOString(),source:$("measurement-source").value});await refreshAssets();message("读数已保存；已开始的排障保留原快照，新排障使用最新读数");});});
action(async()=>{const health=await api("/health");if(health.llm_configured){$("mode").textContent="模型接口已配置";$("mode-note").textContent="仅表示配置已填写，调用结果以会话记录为准。";}else if(!health.demo_data){$("mode").textContent="规则模式 · 模型未接入";$("mode-note").textContent="请导入设备台账与资料，检查结果由人工核验。";}await refreshDevices();await refreshHistory();const id=localStorage.getItem("suzhang-session");if(id){try{await loadSession(id);}catch{localStorage.removeItem("suzhang-session");}}});
