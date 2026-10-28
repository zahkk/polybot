"""Small web dashboard. Binds to localhost by default; open it from your laptop with
    ssh -L 8080:localhost:8080 you@your-vps
and then browse to http://localhost:8080"""

import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse


def build_app(state_fn, actions: dict) -> FastAPI:
    app = FastAPI(title="polybot", docs_url=None, redoc_url=None)

    @app.get("/api/state")
    def api_state():
        return state_fn()

    @app.post("/api/{action}")
    def api_action(action: str):
        fn = actions.get(action)
        if not fn:
            return {"ok": False, "error": "unknown action"}
        return {"ok": True, "result": fn()}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE.replace("{{ts}}", str(int(time.time())))

    return app


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>polybot</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ee;--muted:#8a90a0;--up:#2fbf71;--down:#e5534b;--line:#262a33;--accent:#6ea8fe}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--card:#fff;--fg:#14161a;--muted:#5d6472;--line:#e3e6eb}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;margin-bottom:12px}
h1{font-size:18px;margin:0}.pill{padding:2px 10px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;overflow-x:auto}
.k{color:var(--muted);font-size:12px}.v{font-size:20px;font-variant-numeric:tabular-nums;margin-top:2px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:12px}td.q{white-space:normal;min-width:220px}
.up{color:var(--up)}.down{color:var(--down)}h2{font-size:14px;margin:0 0 8px}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 12px;cursor:pointer}
button.danger{border-color:var(--down);color:var(--down)}section{margin-bottom:12px}
</style></head><body><main>
<header><h1>polybot <span id="mode" class="pill"></span> <span id="status" class="pill"></span></h1>
<div><button onclick="act('pause')">Pause</button> <button onclick="act('resume')">Resume</button>
<button class="danger" onclick="if(confirm('Cancel all orders and stop trading?'))act('kill')">Kill switch</button>
<button onclick="if(confirm('Cancel orders, sell what the bot holds, and shut down?'))act('stop')">Stop &amp; close</button></div></header>
<div class="grid" id="stats"></div>
<section class="card"><h2>Positions</h2><table id="positions"></table></section>
<section class="card"><h2>Open orders</h2><table id="orders"></table></section>
<section class="card"><h2>Claude signals</h2><table id="signals"></table></section>
<section class="card"><h2>Recent fills</h2><table id="fills"></table></section>
<section class="card"><h2>Events</h2><table id="events"></table></section>
</main><script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money=v=>(v<0?'-':'')+'$'+Math.abs(v).toFixed(2);
const cls=v=>v>0?'up':v<0?'down':'';
const t=ts=>new Date(ts*1000).toLocaleString();
function table(el,cols,rows){el.innerHTML='<tr>'+cols.map(c=>'<th>'+c[0]+'</th>').join('')+'</tr>'+
 (rows.length?rows.map(r=>'<tr>'+cols.map(c=>c[1](r)).join('')+'</tr>').join(''):'<tr><td class="k">none</td></tr>')}
async function act(a){await fetch('/api/'+a,{method:'POST'});load()}
async function load(){
 const s=await (await fetch('/api/state')).json();
 $('mode').textContent=s.mode.toUpperCase();
 $('status').textContent=s.killed?'KILLED':s.paused?'PAUSED':'RUNNING';
 $('status').style.color=s.paused?'var(--down)':'var(--up)';
 const net=s.realized+s.rewards_est_total;
 const stats=[['Equity',money(s.equity)],['Cash',money(s.cash)],['Today',money(s.day_pnl),cls(s.day_pnl)],
  ['Realized',money(s.realized),cls(s.realized)],
  ['Rewards today (est.)',money(s.rewards_est_today),cls(s.rewards_est_today)],
  ['Rewards total (est.)',money(s.rewards_est_total),cls(s.rewards_est_total)]];
 if(s.mode==='live')stats.push(['Rewards paid',money(s.rewards_real_total),cls(s.rewards_real_total)]);
 stats.push(['Net (realized + rewards)',money(net),cls(net)],['Exposure',money(s.exposure)],['Claude today',money(s.claude_today)]);
 $('stats').innerHTML=stats.map(x=>`<div class="card"><div class="k">${x[0]}</div><div class="v ${x[2]||''}">${x[1]}</div></div>`).join('');
 table($('positions'),[['Market',r=>`<td class="q">${esc(r.question)}</td>`],['Side',r=>`<td>${esc(r.outcome)}</td>`],
  ['Strategy',r=>`<td>${r.strategy}</td>`],['Shares',r=>`<td>${r.size.toFixed(2)}</td>`],['Avg',r=>`<td>${r.avg_price.toFixed(3)}</td>`],
  ['Now',r=>`<td>${r.mid==null?'-':r.mid.toFixed(3)}</td>`],['P&L',r=>`<td class="${cls(r.pnl)}">${money(r.pnl)}</td>`]],s.positions);
 table($('orders'),[['Market',r=>`<td class="q">${esc(r.question)}</td>`],['Side',r=>`<td>${r.side} ${esc(r.outcome)}</td>`],
  ['Price',r=>`<td>${r.price.toFixed(3)}</td>`],['Shares',r=>`<td>${r.size.toFixed(2)}</td>`],['Strategy',r=>`<td>${r.strategy}</td>`]],s.orders);
 table($('signals'),[['Time',r=>`<td>${t(r.ts)}</td>`],['Market',r=>`<td class="q">${esc(r.question)}</td>`],
  ['Claude',r=>`<td>${(r.prob*100).toFixed(0)}%</td>`],['Edge',r=>`<td class="${cls(r.edge)}">${(r.edge*100).toFixed(1)}c</td>`],
  ['Action',r=>`<td>${r.action}</td>`],['Why',r=>`<td class="q">${esc(r.reasoning)}</td>`]],s.signals);
 table($('fills'),[['Time',r=>`<td>${t(r.ts)}</td>`],['Side',r=>`<td>${r.side}</td>`],['Price',r=>`<td>${r.price.toFixed(3)}</td>`],
  ['Shares',r=>`<td>${r.size.toFixed(2)}</td>`],['Strategy',r=>`<td>${r.strategy}</td>`]],s.fills);
 table($('events'),[['Time',r=>`<td>${t(r.ts)}</td>`],['',r=>`<td>${r.level}</td>`],['',r=>`<td class="q">${esc(r.msg)}</td>`]],s.events);
}
load();setInterval(load,10000);
</script></body></html>"""
