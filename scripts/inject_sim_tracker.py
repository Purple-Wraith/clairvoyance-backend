"""
inject_sim_tracker.py — Simulator + Tracker standalone script injected after main </script>
"""
import sys

SIM_TRACKER_JS = r"""
<script>
/* ================================================================
   CLAIRVOYANCE — SIMULATOR + TRACKER  (standalone, outside IIFE)
   ================================================================ */

/* ── CV_META: centralized sport/league registry ─────────────────
   Add new leagues here — everything else auto-adapts.
   ──────────────────────────────────────────────────────────────── */
var CV_META = {
  /* sport categories */
  sports: {
    baseball:   { label:'BASEBALL',   short:'BASE', color:'var(--hc)' },
    basketball: { label:'BASKETBALL', short:'BBALL',color:'var(--pc)' },
    hockey:     { label:'HOCKEY',     short:'HCKY', color:'var(--ic)' },
    football:   { label:'FOOTBALL',   short:'FTBL', color:'var(--gc)' },
    soccer:     { label:'SOCCER',     short:'SOC',  color:'var(--nc)' }
  },
  /* league definitions — add any new league here */
  leagues: {
    MLB:       { label:'MLB',          sport:'baseball',   color:'var(--hc)' },
    NBA:       { label:'NBA',          sport:'basketball', color:'var(--pc)' },
    WNBA:      { label:'WNBA',         sport:'basketball', color:'var(--vc)' },
    NFL:       { label:'NFL',          sport:'football',   color:'var(--gc)' },
    CFB:       { label:'CFB',          sport:'football',   color:'var(--rc)' },
    NHL:       { label:'NHL',          sport:'hockey',     color:'var(--ic)' },
    CH:        { label:'COLLEGE HCKY', sport:'hockey',     color:'var(--vc)' },
    SHL:       { label:'SHL',          sport:'hockey',     color:'var(--nc)' },
    LIIGA:     { label:'LIIGA',        sport:'hockey',     color:'var(--rc)' },
    WORLD_CUP: { label:'WORLD CUP',    sport:'soccer',     color:'var(--gc)' }
  },
  /* sport code → league (for picks stored with sport only) */
  sportToLeague: {
    MLB:'MLB',NBA:'NBA',WNBA:'WNBA',NFL:'NFL',CFB:'CFB',
    NHL:'NHL',SHL:'SHL',LIIGA:'LIIGA',CH:'CH',
    SOC:'WORLD_CUP',WORLDCUP:'WORLD_CUP'
  }
};

/* ── Tag rendering helpers ───────────────────────────────────────── */
function cvLeagueTag(leagueKey, extra) {
  var lk  = (leagueKey||'').toUpperCase().replace('-','_');
  var def = CV_META.leagues[lk];
  var col = def ? def.color : 'var(--t3)';
  var lbl = def ? def.label : (leagueKey || '?');
  return '<span style="font-family:var(--mono);font-size:10px;padding:1px 6px;border:1px solid '+col+';color:'+col+';border-radius:2px;white-space:nowrap'+(extra?';'+extra:'')+'">'+lbl+'</span>';
}
function cvSportTag(leagueKey, extra) {
  var lk   = (leagueKey||'').toUpperCase().replace('-','_');
  var def  = CV_META.leagues[lk];
  var sKey = def ? def.sport : null;
  var sprt = sKey ? CV_META.sports[sKey] : null;
  if (!sprt) return '';
  return '<span style="font-family:var(--mono);font-size:9px;padding:1px 5px;border:1px solid rgba(255,255,255,.15);color:var(--t3);border-radius:2px;white-space:nowrap'+(extra?';'+extra:'')+'">'+sprt.label+'</span>';
}
function cvBetTypeTag(betType) {
  var map = {ML:'MONEYLINE',RL:'RUN LINE',PL:'PUCK LINE',OU:'O/U',SPREAD:'SPREAD',PROP:'PROP',PARLAY:'PARLAY'};
  var lbl = map[(betType||'').toUpperCase()] || betType || 'BET';
  return '<span style="font-family:var(--mono);font-size:10px;color:var(--t3)">'+lbl+'</span>';
}
function cvParlayTag(legCount) {
  return '<span style="font-family:var(--mono);font-size:10px;padding:1px 6px;border:1px solid var(--pc);color:var(--pc);background:rgba(240,0,255,.08);border-radius:2px;font-weight:700">PARLAY '+(legCount?legCount+'-LEG':'')+'</span>';
}
function cvParlayLegTag(legNum) {
  return '<span style="font-family:var(--mono);font-size:9px;padding:1px 4px;border:1px solid rgba(240,0,255,.4);color:rgba(240,0,255,.7);border-radius:2px">LEG '+legNum+'</span>';
}

/* resolve league from a pick object */
function cvPickLeague(p) {
  if (p.league) return p.league.toUpperCase().replace('-','_');
  /* re-derive from sport code */
  var s = (p.sport || '').toUpperCase();
  return CV_META.sportToLeague[s] || s || 'OTHER';
}

/* ── Fallback team lists ─────────────────────────────────────────── */
var SIM_FB = {
  mlb:['ARI','ATL','BAL','BOS','CHC','CWS','CIN','CLE','COL','DET','HOU','KC','LAA','LAD','MIA','MIL','MIN','NYM','NYY','OAK','PHI','PIT','SD','SEA','SF','STL','TB','TEX','TOR','WSN'],
  nba:['ATL','BKN','BOS','CHA','CHI','CLE','DAL','DEN','DET','GS','HOU','IND','LAC','LAL','MEM','MIA','MIL','MIN','NOP','NY','OKC','ORL','PHI','PHX','POR','SAC','SA','TOR','UTA','WAS'],
  nhl:['ANA','BOS','BUF','CAR','CBJ','CGY','CHI','COL','DAL','DET','EDM','FLA','LAK','MIN','MTL','NJD','NSH','NYI','NYR','OTT','PHI','PIT','SEA','SJS','STL','TBL','TOR','VAN','VGK','WPG','WSH'],
  wnba:['ATL','CHI','CON','DAL','IND','LA','LV','MIN','NY','PHX','SEA','WSH']
};

/* ── simUpdateTeams ──────────────────────────────────────────────── */
function simUpdateTeams() {
  var sel  = document.getElementById('sim-sport');
  var hSel = document.getElementById('sim-home');
  var aSel = document.getElementById('sim-away');
  if (!sel || !hSel || !aSel) return;
  var sport = sel.value || 'mlb';
  var teams = [];
  try {
    if (sport === 'mlb')    teams = Object.keys(typeof MLB !== 'undefined' ? MLB : {});
    else if (sport === 'nba')   teams = Object.keys(typeof NBA_TEAMS !== 'undefined' ? NBA_TEAMS : {});
    else if (sport === 'nhl') {
      var _n = typeof NHL !== 'undefined' ? NHL : {};
      teams = Object.keys(_n).filter(function(k){ return !(_n[k] && (_n[k].series||'').indexOf('ELIMINATED') >= 0); });
    }
    else if (sport === 'wnba') {
      var D = window.__CV_DATA || {};
      teams = Object.keys((D.wnba && D.wnba.standings) ? D.wnba.standings : {});
    }
  } catch(e) {}
  if (teams.length < 2) teams = SIM_FB[sport] || SIM_FB.mlb;
  teams = teams.slice().sort();
  var opts = teams.map(function(t){ return '<option value="'+t+'">'+t+'</option>'; }).join('');
  hSel.innerHTML = opts;
  aSel.innerHTML = opts;
  if (teams.length > 1) aSel.selectedIndex = 1;
}

/* ── runSimulator ───────────────────────────────────────────────── */
function runSimulator() {
  var sport = (document.getElementById('sim-sport')||{}).value || 'mlb';
  var home  = (document.getElementById('sim-home') ||{}).value;
  var away  = (document.getElementById('sim-away') ||{}).value;
  var N     = parseInt((document.getElementById('sim-count')||{}).value||'10000');
  var flags = {
    injury:  (document.getElementById('sim-inc-injury') ||{}).checked !== false,
    home:    (document.getElementById('sim-inc-home')   ||{}).checked !== false,
    form:    (document.getElementById('sim-inc-form')   ||{}).checked !== false,
    rest:    (document.getElementById('sim-inc-rest')   ||{}).checked !== false,
    weather: !!(document.getElementById('sim-inc-weather')||{}).checked
  };
  var el = document.getElementById('sim-results');
  if (!el) return;
  if (!home||!away){ el.innerHTML='<div class="empty">Select two teams and click RUN SIMULATION</div>'; return; }
  if (home===away){ el.innerHTML='<div class="empty">Teams must be different</div>'; return; }
  el.innerHTML='<div style="font-family:var(--mono);font-size:13px;color:var(--nc);text-align:center;padding:40px"><span class="spi"></span>&nbsp; RUNNING '+N.toLocaleString()+' SIMULATIONS…</div>';
  setTimeout(function(){
    try { var res=_simRun(sport,home,away,N,flags); _simRender(el,sport,home,away,N,res); }
    catch(e){ el.innerHTML='<div class="empty">SIM ERROR: '+e.message+'</div>'; console.error(e); }
  },50);
}

/* ── _simRun ────────────────────────────────────────────────────── */
function _simRun(sport,home,away,N,flags){
  var baseHP=0.5;
  try{
    if(sport==='mlb' &&typeof mlbEns==='function'){var e=mlbEns(home,away); if(e&&e.p)baseHP=e.p;}
    if(sport==='nba' &&typeof nbaEns==='function'){var e=nbaEns(home,away); if(e&&e.p)baseHP=e.p;}
    if(sport==='nhl' &&typeof nhlEns==='function'){var e=nhlEns(home,away); if(e&&e.p)baseHP=e.p;}
    if(sport==='wnba'){var D=window.__CV_DATA||{},ts=(D.wnba&&D.wnba.teamStats)||{},hn=ts[home],an=ts[away];
      if(hn&&an){var diff=((hn.ortg||100)-(hn.drtg||100))-((an.ortg||100)-(an.drtg||100));baseHP=1/(1+Math.exp(-diff/12));}}
  }catch(e){}
  var adjHP=baseHP;
  if(flags.home)adjHP=Math.min(.88,adjHP*1.035);
  if(flags.form)adjHP=Math.min(.88,Math.max(.12,adjHP+(Math.random()-.48)*.02));
  if(flags.rest)adjHP=Math.min(.88,Math.max(.12,adjHP+(Math.random()-.48)*.015));
  adjHP=Math.min(.88,Math.max(.12,adjHP));
  var SP={mlb:{muH:4.5,muA:4.2,sdH:1.8,sdA:1.7,ou:8.5},nba:{muH:111,muA:108,sdH:10,sdA:10,ou:220},
    nhl:{muH:3.0,muA:2.7,sdH:1.2,sdA:1.1,ou:5.5},wnba:{muH:82,muA:80,sdH:8,sdA:8,ou:162}};
  var sp=SP[sport]||SP.mlb;
  var hWins=0,aWins=0,hSc=[],aSc=[],tots=[],marg=[];
  for(var i=0;i<N;i++){
    var u1=Math.random()||1e-9,u2=Math.random()||1e-9;
    var z1=Math.sqrt(-2*Math.log(u1))*Math.cos(2*Math.PI*u2);
    var z2=Math.sqrt(-2*Math.log(u1))*Math.sin(2*Math.PI*u2);
    var hAdj=adjHP>.5?(adjHP-.5)*sp.muH*.3:0,aAdj=adjHP<.5?(.5-adjHP)*sp.muA*.3:0;
    var hs=Math.max(0,sp.muH+hAdj+z1*sp.sdH),as2=Math.max(0,sp.muA+aAdj+z2*sp.sdA);
    if(sport==='mlb'||sport==='nhl'){hs=Math.round(hs);as2=Math.round(as2);}
    else{hs=Math.round(hs*10)/10;as2=Math.round(as2*10)/10;}
    hSc.push(hs);aSc.push(as2);tots.push(hs+as2);marg.push(hs-as2);
    if(hs>as2)hWins++;else if(as2>hs)aWins++;
  }
  function mean(a){return a.reduce(function(s,v){return s+v;},0)/a.length;}
  function std(a){var m=mean(a);return Math.sqrt(a.reduce(function(s,v){return s+(v-m)*(v-m);},0)/a.length);}
  function pct(a,p){var s=a.slice().sort(function(x,y){return x-y;});return s[Math.floor(p/100*s.length)]||0;}
  function dist(arr,B){var mn=Math.min.apply(null,arr),mx=Math.max.apply(null,arr),bw=(mx-mn)/B||1,bins=[];
    for(var j=0;j<B;j++)bins.push(0);
    arr.forEach(function(v){var bi=Math.min(B-1,Math.floor((v-mn)/bw));bins[bi]++;});
    return{bins:bins,mn:mn,bw:bw};}
  var hM=mean(hSc),aM=mean(aSc),blk=Math.floor(N/20),trend=[];
  for(var b=0;b<20;b++){var sl=hSc.slice(b*blk,(b+1)*blk),sl2=aSc.slice(b*blk,(b+1)*blk);
    trend.push(+(sl.filter(function(v,i){return v>sl2[i];}).length/sl.length).toFixed(3));}
  var outliers=hSc.map(function(h,i){return{h:h,a:aSc[i]};})
    .filter(function(g){return Math.abs(g.h-hM)>2*std(hSc)||Math.abs(g.a-aM)>2*std(aSc);}).slice(0,6);
  return{N:N,hP:hWins/N,aP:aWins/N,hMean:hM,aMean:aM,hStd:std(hSc),aStd:std(aSc),
    totMean:mean(tots),totStd:std(tots),overP:tots.filter(function(t){return t>sp.ou;}).length/N,
    blowoutP:marg.filter(function(m){return Math.abs(m)>10;}).length/N,
    p10h:pct(hSc,10),p90h:pct(hSc,90),p10a:pct(aSc,10),p90a:pct(aSc,90),
    p10t:pct(tots,10),p90t:pct(tots,90),
    hDist:dist(hSc,12),aDist:dist(aSc,12),totDist:dist(tots,12),
    trend:trend,outliers:outliers,ou:sp.ou};}

/* ── _simRender ─────────────────────────────────────────────────── */
function _simRender(el,sport,home,away,N,r){
  var p2ml_=typeof p2ml==='function'?p2ml:function(p){return p>=.5?-Math.round(100*p/(1-p)):'+'+Math.round(100*(1-p)/p);};
  var ml2d_=typeof ml2d==='function'?ml2d:function(ml){var m=parseFloat(ml);return m>0?m/100+1:100/Math.abs(m)+1;};
  var ev_  =typeof ev==='function'?ev:function(p,d){return+(p*d-1).toFixed(4);};
  var td   =typeof today==='function'?today():new Date().toISOString().slice(0,10).replace(/-/g,'');
  var hML=p2ml_(r.hP),aML=p2ml_(r.aP),hD=ml2d_(hML),aD=ml2d_(aML);
  var hEV=ev_(r.hP,hD),aEV=ev_(r.aP,aD);
  var favTeam=r.hP>=r.aP?home:away,favP=Math.max(r.hP,r.aP),favEV=r.hP>=r.aP?hEV:aEV;
  var tier=favP>=.67&&favEV>=.05?'ELITE':favP>=.62&&favEV>=.03?'LOCK':favP>=.55?'LEAN':'EDGE';
  var tierCol=tier==='ELITE'?'var(--gc)':tier==='LOCK'?'var(--nc)':tier==='LEAN'?'var(--ic)':'var(--t3)';
  var unit={mlb:'runs',nba:'pts',nhl:'goals',wnba:'pts'}[sport]||'pts';
  var tMin=Math.min.apply(null,r.trend),tMax=Math.max.apply(null,r.trend),tOK=(tMax-tMin)<.09;
  /* League tag for sim header */
  var simLeague={mlb:'MLB',nba:'NBA',nhl:'NHL',wnba:'WNBA'}[sport]||sport.toUpperCase();
  function bars(dist,col){return dist.bins.map(function(b,i){
    var lo=(dist.mn+i*dist.bw).toFixed(1),h=Math.round(b/Math.max.apply(null,dist.bins)*60)+4;
    return'<div title="'+lo+': '+((b/r.N)*100).toFixed(1)+'%" style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end">'
      +'<div style="width:100%;background:'+col+';height:'+h+'px;border-radius:1px 1px 0 0;opacity:.75"></div>'
      +'<div style="font-size:7px;color:var(--t3);writing-mode:vertical-lr;transform:rotate(180deg);margin-top:2px">'+lo+'</div>'
      +'</div>'}).join('');}
  var html=
    '<div style="background:rgba(4,0,12,.6);border:1px solid rgba(240,0,255,.25);border-radius:3px;padding:12px 14px;margin-bottom:10px">'
    +'<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:8px">'
      +cvLeagueTag(simLeague)+cvSportTag(simLeague)
      +'<span style="font-family:var(--mono);font-size:10px;color:var(--t3)">'+N.toLocaleString()+' SIMULATIONS &middot; '+away+' at '+home+'</span>'
    +'</div>'
    +'<div style="display:flex;gap:10px;flex-wrap:wrap">'
      +'<div style="flex:1;min-width:110px;text-align:center;background:rgba(240,0,255,.07);border:1px solid rgba(240,0,255,.2);border-radius:2px;padding:10px 6px">'
        +'<div style="font-family:var(--orb);font-size:22px;font-weight:900;color:var(--pc)">'+(r.hP*100).toFixed(1)+'%</div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t2);margin-top:2px">'+home+' WIN</div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t3)">'+(hML>0?'+':'')+hML+' &middot; EV '+(hEV*100).toFixed(1)+'%</div>'
      +'</div>'
      +'<div style="flex:0 0 80px;text-align:center;padding:10px 4px">'
        +'<div style="font-family:var(--orb);font-size:13px;color:var(--t3)">vs</div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t3);margin-top:4px">O/U '+r.ou+'</div>'
        +'<div style="font-family:var(--mono);font-size:13px;font-weight:700;color:'+(r.overP>=.5?'var(--nc)':'var(--hc)')+'">'+( r.overP*100).toFixed(1)+'%</div>'
        +'<div style="font-family:var(--mono);font-size:10px;color:var(--t3)">'+(r.overP>=.5?'OVER':'UNDER')+'</div>'
      +'</div>'
      +'<div style="flex:1;min-width:110px;text-align:center;background:rgba(0,240,255,.07);border:1px solid rgba(0,240,255,.2);border-radius:2px;padding:10px 6px">'
        +'<div style="font-family:var(--orb);font-size:22px;font-weight:900;color:var(--nc)">'+(r.aP*100).toFixed(1)+'%</div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t2);margin-top:2px">'+away+' WIN</div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t3)">'+(aML>0?'+':'')+aML+' &middot; EV '+(aEV*100).toFixed(1)+'%</div>'
      +'</div>'
    +'</div></div>'
    +'<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px">'
      +'<div class="card"><div style="font-family:var(--mono);font-size:10px;color:var(--pc);letter-spacing:2px;margin-bottom:4px">'+home+' SCORE</div>'
        +'<div style="font-family:var(--orb);font-size:20px;font-weight:900">'+r.hMean.toFixed(1)+'</div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t3)">&plusmn;'+r.hStd.toFixed(1)+' '+unit+' &middot; P10–90: '+r.p10h.toFixed(1)+'–'+r.p90h.toFixed(1)+'</div>'
        +'<div style="display:flex;gap:1px;height:68px;align-items:flex-end;margin-top:6px">'+bars(r.hDist,'var(--pc)')+'</div>'
      +'</div>'
      +'<div class="card"><div style="font-family:var(--mono);font-size:10px;color:var(--gc);letter-spacing:2px;margin-bottom:4px">TOTAL</div>'
        +'<div style="font-family:var(--orb);font-size:20px;font-weight:900">'+r.totMean.toFixed(1)+'</div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t3)">&plusmn;'+r.totStd.toFixed(1)+' '+unit+' &middot; P10–90: '+r.p10t.toFixed(1)+'–'+r.p90t.toFixed(1)+'</div>'
        +'<div style="display:flex;gap:1px;height:68px;align-items:flex-end;margin-top:6px">'+bars(r.totDist,'var(--gc)')+'</div>'
      +'</div>'
      +'<div class="card"><div style="font-family:var(--mono);font-size:10px;color:var(--nc);letter-spacing:2px;margin-bottom:4px">'+away+' SCORE</div>'
        +'<div style="font-family:var(--orb);font-size:20px;font-weight:900">'+r.aMean.toFixed(1)+'</div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t3)">&plusmn;'+r.aStd.toFixed(1)+' '+unit+' &middot; P10–90: '+r.p10a.toFixed(1)+'–'+r.p90a.toFixed(1)+'</div>'
        +'<div style="display:flex;gap:1px;height:68px;align-items:flex-end;margin-top:6px">'+bars(r.aDist,'var(--nc)')+'</div>'
      +'</div>'
    +'</div>'
    +'<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">'
      +'<div class="card"><div style="font-family:var(--mono);font-size:10px;color:var(--vc);letter-spacing:2px;margin-bottom:4px">WIN PROB TREND <span style="color:'+(tOK?'var(--nc)':'var(--hc)')+'">// '+(tOK?'STABLE':'VOLATILE')+'</span></div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t3);margin-bottom:4px">20-block range: '+((tMax-tMin)*100).toFixed(1)+'%</div>'
        +'<div style="display:flex;gap:2px;height:50px;align-items:flex-end">'
          +r.trend.map(function(v){return'<div style="flex:1;background:'+(v>=.5?'var(--nc)':'var(--hc)')+';height:'+Math.round(v*50)+'px;border-radius:1px 1px 0 0;opacity:.7" title="'+(v*100).toFixed(1)+'%"></div>';}).join('')
        +'</div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t2);margin-top:4px">Peak <span style="color:var(--nc)">'+(tMax*100).toFixed(1)+'%</span> &middot; Floor <span style="color:var(--hc)">'+(tMin*100).toFixed(1)+'%</span></div>'
      +'</div>'
      +'<div class="card"><div style="font-family:var(--mono);font-size:10px;color:var(--hc);letter-spacing:2px;margin-bottom:4px">OUTLIERS <span style="font-size:9px;color:var(--t3)">&gt;2&sigma;</span></div>'
        +'<div style="font-family:var(--mono);font-size:11px;color:var(--t3);margin-bottom:4px">'+r.outliers.length+' extreme &middot; Blowout rate: '+(r.blowoutP*100).toFixed(1)+'%</div>'
        +(r.outliers.length?r.outliers.map(function(o){return'<div style="display:flex;justify-content:space-between;font-family:var(--mono);font-size:12px;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.04)"><span style="color:var(--pc)">'+home+' '+(Number.isInteger(o.h)?o.h:o.h.toFixed(1))+'</span><span style="color:var(--nc)">'+away+' '+(Number.isInteger(o.a)?o.a:o.a.toFixed(1))+'</span><span style="color:'+(o.h>o.a?'var(--pc)':'var(--nc)')+'">'+( o.h>o.a?home:away)+'</span></div>';}).join(''):'<div style="font-family:var(--mono);font-size:12px;color:var(--t3)">Model stable — no extreme outliers</div>')
      +'</div>'
    +'</div>'
    +'<div class="card" style="margin-bottom:10px"><div style="font-family:var(--mono);font-size:10px;color:var(--gc);letter-spacing:2px;margin-bottom:8px">// ENGINE FINDINGS</div>'
      +'<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'
        +'<div style="font-family:var(--mono);font-size:12px;color:var(--t2);line-height:1.9">'
          +'<span style="color:'+tierCol+'">'+tier+': '+favTeam+'</span><br>'
          +'Win prob: <strong>'+( favP*100).toFixed(1)+'%</strong><br>'
          +'Proj margin: <strong>'+Math.abs(r.hMean-r.aMean).toFixed(1)+' '+unit+'</strong> &rarr; '+(r.hMean>r.aMean?home:away)+'<br>'
          +'O/U: <strong style="color:'+(r.overP>=.54?'var(--nc)':'var(--hc)')+'">LEAN '+(r.overP>=.54?'OVER':'UNDER')+' '+r.ou+'</strong> ('+(r.overP*100).toFixed(1)+'%)<br>'
          +'EV: <strong style="color:'+(favEV>=0?'var(--nc)':'var(--hc)')+'">'+( favEV>=0?'+':'')+( favEV*100).toFixed(1)+'%</strong>'
        +'</div>'
        +'<div style="font-family:var(--mono);font-size:12px;color:var(--t2);line-height:1.9">'
          +'Variance: <strong>'+r.hStd.toFixed(1)+' / '+r.aStd.toFixed(1)+'</strong><br>'
          +'Blowout risk: <strong style="color:'+(r.blowoutP>.25?'var(--hc)':'var(--t3)')+'">'+( r.blowoutP*100).toFixed(1)+'%</strong><br>'
          +'Trend: <strong style="color:'+(tOK?'var(--nc)':'var(--hc)')+'">'+( tOK?'STABLE':'MIXED')+'</strong><br>'
          +'Sims: <strong>'+N.toLocaleString()+'</strong><br>'
          +'Confidence: <strong style="color:'+tierCol+'">'+tier+'</strong>'
        +'</div>'
      +'</div>'
    +'</div>'
    +'<div style="display:flex;gap:8px;flex-wrap:wrap">'
      +'<button class="btn btn-p btn-sm" id="sim-lock-h">LOCK '+home+' ML '+(hML>0?'+':'')+hML+'</button>'
      +'<button class="btn btn-o btn-sm" id="sim-lock-a">LOCK '+away+' ML '+(aML>0?'+':'')+aML+'</button>'
      +'<button class="btn btn-o btn-sm" id="sim-lock-ou">LOCK '+(r.overP>=.5?'OVER':'UNDER')+' '+r.ou+'</button>'
    +'</div>';
  el.innerHTML=html;
  setTimeout(function(){
    var bH=document.getElementById('sim-lock-h'),bA=document.getElementById('sim-lock-a'),bOU=document.getElementById('sim-lock-ou');
    var ouDir=r.overP>=.5?'OVER':'UNDER';
    if(bH &&typeof lockPick==='function')bH.onclick =function(){lockPick(home,away,'ML',home,r.hP,hML,hD,td);};
    if(bA &&typeof lockPick==='function')bA.onclick =function(){lockPick(home,away,'ML',away,r.aP,aML,aD,td);};
    if(bOU&&typeof lockPick==='function')bOU.onclick=function(){lockPick(home,away,'OU',ouDir+' '+r.ou,r.overP,'-110',1.91,td);};
  },80);
}

/* ================================================================
   TRACKER — Locked Bets
   ================================================================ */
function renderLockedTracker() {
  var el = document.getElementById('tracker-locked-list');
  if (!el) return;
  var sportF  = (document.getElementById('tracker-filter-sport') ||{}).value||'all';
  var statusF = (document.getElementById('tracker-filter-status')||{}).value||'all';
  var picks   = typeof getP==='function' ? getP() : [];
  if (!picks.length) {
    el.innerHTML='<div class="empty">No locked bets yet.<br><span style="font-size:13px;color:var(--t3)">Lock picks from any game card to track them here.</span></div>';
    return;
  }

  /* NHL team set for re-inference */
  var NHL_T=new Set(['ANA','BOS','BUF','CAR','CBJ','CGY','CHI','COL','DAL','DET','EDM','FLA','LAK','MIN','MTL','NJD','NSH','NYI','NYR','OTT','PHI','PIT','SEA','SJS','STL','TBL','TOR','VAN','VGK','WPG','WSH']);
  var MLB_T=new Set(['ARI','ATL','BAL','BOS','CHC','CWS','CIN','CLE','COL','DET','HOU','KC','LAA','LAD','MIA','MIL','MIN','NYM','NYY','OAK','PHI','PIT','SD','SEA','SF','STL','TB','TEX','TOR','WSN']);

  picks = picks.map(function(p){
    if((!p.sport||p.sport==='MLB')&&(NHL_T.has(p.hA)||NHL_T.has(p.awA)))return Object.assign({},p,{sport:'NHL',league:'NHL'});
    return p;
  });

  if(sportF!=='all') picks=picks.filter(function(p){return(p.sport||'').toUpperCase()===sportF.toUpperCase();});
  if(statusF!=='all')picks=picks.filter(function(p){return(p.outcome||'pending')===statusF;});
  picks=picks.slice().sort(function(a,b){return(b.lockedAt||0)-(a.lockedAt||0);});

  if(!picks.length){el.innerHTML='<div class="empty">No bets match this filter</div>';return;}

  /* Build parlay lookup from cv_parlays */
  var parlayLegs = {}; /* pickId → {parlayId, legIndex, legTotal} */
  try {
    var parlays = JSON.parse(localStorage.getItem('cv_parlays')||'[]');
    parlays.forEach(function(parl){
      (parl.legs||[]).forEach(function(leg,i){
        if(leg.pickId) parlayLegs[leg.pickId] = {parlayId:parl.id, legN:i+1, total:parl.legs.length};
      });
    });
  }catch(e){}

  /* Group by date */
  var byDate={};
  picks.forEach(function(p){var d=p.date||'unknown';if(!byDate[d])byDate[d]=[];byDate[d].push(p);});
  var dates=Object.keys(byDate).sort(function(a,b){return b.localeCompare(a);});
  var html='';

  dates.forEach(function(date){
    html+='<div style="font-family:var(--mono);font-size:10px;color:var(--t3);letter-spacing:3px;margin:10px 0 5px">'+date+'</div>';
    byDate[date].forEach(function(p){
      var league   = cvPickLeague(p);
      var outcome  = p.outcome||'pending';
      var isPend   = outcome==='pending';
      var isWin    = outcome==='win';
      var isParlay = p.betType==='PARLAY';
      var borderC  = isPend?'var(--nc)':isWin?'var(--gc)':'var(--hc)';
      var prob     = Math.round((p.winProb||.5)*100);
      var probCol  = prob>=67?'var(--gc)':prob>=55?'var(--nc)':'var(--ic)';
      var mlStr    = p.ml?(parseFloat(p.ml)>0?'+':'')+p.ml:'—';
      var statBadge= isPend
        ?'<span style="font-family:var(--mono);font-size:10px;padding:1px 6px;border:1px solid var(--nc);color:var(--nc);border-radius:2px">PENDING</span>'
        :isWin
          ?'<span style="font-family:var(--mono);font-size:10px;padding:1px 6px;border:1px solid var(--gc);color:var(--gc);border-radius:2px">WIN</span>'
          :'<span style="font-family:var(--mono);font-size:10px;padding:1px 6px;border:1px solid var(--hc);color:var(--hc);border-radius:2px">LOSS</span>';
      var evStr = (p.decOdds&&p.winProb)?((p.winProb*p.decOdds-1)*100).toFixed(1):null;
      /* Parlay indicator */
      var parlayInfo = parlayLegs[p.id];
      var parlayBadge = isParlay
        ? cvParlayTag(p.betOn ? (p.betOn.split('|').length) : '')
        : (parlayInfo ? cvParlayLegTag(parlayInfo.legN) : '');
      /* Legs breakdown for PARLAY bets */
      var legsHtml = '';
      if(isParlay && p.betOn){
        var legs=p.betOn.split('|');
        legsHtml='<div style="margin-top:6px;border-top:1px solid rgba(255,255,255,.08);padding-top:6px">'
          +'<div style="font-family:var(--mono);font-size:10px;color:var(--t3);letter-spacing:2px;margin-bottom:4px">LEGS ('+legs.length+')</div>'
          +legs.map(function(leg,i){
            return'<div style="display:flex;align-items:center;gap:5px;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.04)">'
              +cvParlayLegTag(i+1)
              +'<span style="font-family:var(--mono);font-size:12px;color:var(--t)">'+leg+'</span>'
              +'</div>';
          }).join('')
          +'</div>';
      }
      var settleBtns = isPend
        ?'<div style="display:flex;gap:4px;margin-top:6px">'
          +'<button class="btn btn-sm" style="color:var(--gc);border-color:var(--gc);font-size:10px" onclick="recR(\''+p.id+'\',\'win\');setTimeout(renderLockedTracker,400)">WIN</button>'
          +'<button class="btn btn-sm" style="color:var(--hc);border-color:var(--hc);font-size:10px" onclick="recR(\''+p.id+'\',\'loss\');setTimeout(renderLockedTracker,400)">LOSS</button>'
          +'<button class="btn btn-o btn-sm" style="font-size:10px" onclick="recR(\''+p.id+'\',\'push\');setTimeout(renderLockedTracker,400)">PUSH</button>'
          +'</div>':'' ;
      html+=
        '<div class="gc" style="border-left:2px solid '+borderC+';margin-bottom:8px">'
          +'<div class="gch">'
            +'<div>'
              +'<div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:4px">'
                +cvLeagueTag(league)
                +cvSportTag(league)
                +cvBetTypeTag(p.betType)
                +statBadge
                +parlayBadge
              +'</div>'
              +'<div class="gtm" style="font-size:17px">'+p.betOn+'</div>'
              +'<div class="gsp">'+(p.awA||'')+((p.awA&&p.hA)?' at ':'')+( p.hA||'')+' &middot; '+mlStr+'</div>'
            +'</div>'
            +'<div style="text-align:right">'
              +'<div style="font-family:var(--orb);font-size:18px;font-weight:900;color:'+probCol+'">'+prob+'%</div>'
              +'<div style="font-family:var(--mono);font-size:10px;color:var(--t3)">WIN PROB</div>'
            +'</div>'
          +'</div>'
          +'<div style="padding:6px 10px 8px">'
            +'<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
              +'<div style="font-family:var(--mono);font-size:9px;color:var(--t3)">PROB</div>'
              +'<div style="flex:1;height:5px;background:rgba(255,255,255,.08);border-radius:3px;overflow:hidden">'
                +'<div style="height:100%;width:'+prob+'%;background:'+probCol+';border-radius:3px"></div>'
              +'</div>'
              +'<div style="font-family:var(--mono);font-size:9px;color:'+probCol+'">'+prob+'%</div>'
            +'</div>'
            +'<div style="font-family:var(--mono);font-size:11px;color:var(--t3)">'
              +(p.lockedAt?'Locked: '+new Date(p.lockedAt).toLocaleString():'')
              +(evStr!==null?' &middot; EV: '+(parseFloat(evStr)>=0?'+':'')+evStr+'%':'')
            +'</div>'
            +legsHtml
            +settleBtns
          +'</div>'
        +'</div>';
    });
  });
  el.innerHTML=html;
}

/* ================================================================
   TRACKER — Parlay
   ================================================================ */
function renderParlayTracker() {
  var el = document.getElementById('tracker-parlay-list');
  if (!el) return;
  var filt=(document.getElementById('tracker-parlay-filter')||{}).value||'all';
  var parlays=[];
  try{parlays=JSON.parse(localStorage.getItem('cv_parlays')||'[]');}catch(e){}

  /* Inject live PAR legs */
  if(typeof PAR!=='undefined'&&PAR&&PAR.length>=2){
    var legs=PAR.map(function(l){return{betOn:l.name,ml:l.ml,prob:l.prob||.5,matchup:l.matchup,type:'ML',sport:'MLB',league:'MLB',outcome:'pending'};});
    var cD=legs.reduce(function(a,l){return a*(typeof ml2d==='function'?ml2d(l.ml):1.91);},1);
    var cP=legs.reduce(function(a,l){return a*(l.prob||.5);},1);
    if(!parlays.find(function(p){return p.id==='cur_mlb';}))
      parlays.unshift({id:'cur_mlb',date:typeof today==='function'?today():'today',sport:'MLB',league:'MLB',legs:legs,combinedDec:+cD.toFixed(3),combinedProb:+cP.toFixed(6),combinedML:Math.round(cD>2?(cD-1)*100:-(100/(cD-1))),outcome:'pending',lockedAt:Date.now()});
  }
  if(typeof NBA_PAR!=='undefined'&&NBA_PAR&&NBA_PAR.length>=2){
    var legs=NBA_PAR.map(function(l){return{betOn:l.name,ml:l.ml,prob:l.prob||.5,matchup:l.matchup,type:'ML',sport:'NBA',league:'NBA',outcome:'pending'};});
    var cD=legs.reduce(function(a,l){return a*(typeof ml2d==='function'?ml2d(l.ml):1.91);},1);
    var cP=legs.reduce(function(a,l){return a*(l.prob||.5);},1);
    if(!parlays.find(function(p){return p.id==='cur_nba';}))
      parlays.unshift({id:'cur_nba',date:typeof today==='function'?today():'today',sport:'NBA',league:'NBA',legs:legs,combinedDec:+cD.toFixed(3),combinedProb:+cP.toFixed(6),combinedML:Math.round(cD>2?(cD-1)*100:-(100/(cD-1))),outcome:'pending',lockedAt:Date.now()});
  }

  if(filt!=='all')parlays=parlays.filter(function(p){return(p.outcome||'pending')===filt;});
  if(!parlays.length){
    el.innerHTML='<div class="empty">No parlays yet.<br><span style="font-size:13px;color:var(--t3)">Build a parlay using the +PAR buttons on any game card.</span></div>';
    return;
  }

  var TYPE_LBL={ML:'ML',RL:'RL',PL:'PL',OU:'O/U',SPREAD:'SPR',PROP:'PROP'};
  var html='';

  parlays.forEach(function(parl){
    var outcome  = parl.outcome||'pending';
    var borderC  = outcome==='win'?'var(--gc)':outcome==='loss'?'var(--hc)':'var(--pc)';
    var combML   = parl.combinedML||0;
    var combP    = parl.combinedProb||parl.legs.reduce(function(a,l){return a*(l.prob||.5);},1);
    var legsDone = parl.legs.filter(function(l){return l.outcome==='win';}).length;
    var legsLost = parl.legs.filter(function(l){return l.outcome==='loss';}).length;
    var legsPend = parl.legs.filter(function(l){return(l.outcome||'pending')==='pending';}).length;
    var ev_parl  = (combP*(parl.combinedDec||1)-1)*100;
    /* Determine if multi-sport parlay */
    var sports=[...new Set(parl.legs.map(function(l){return l.sport||l.league||'';}).filter(Boolean))];
    var isMulti = sports.length > 1;

    html+=
      '<div class="gc" style="border-left:2px solid '+borderC+';margin-bottom:12px">'
        +'<div class="gch">'
          +'<div>'
            +'<div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:4px">'
              +cvParlayTag(parl.legs.length)
              +(isMulti?'<span style="font-family:var(--mono);font-size:10px;color:var(--t3)">MULTI-SPORT</span>':cvLeagueTag(parl.league||parl.sport||''))
              +'<span style="font-family:var(--mono);font-size:10px;color:var(--t3)">'+parl.date+'</span>'
            +'</div>'
            +'<div class="gtm" style="font-size:16px;color:var(--pc)">'+(combML>0?'+':'')+combML+' &middot; '+(combP*100).toFixed(2)+'% WIN PROB</div>'
            +'<div class="gsp">'+legsDone+'/'+parl.legs.length+' legs won &middot; '+legsLost+' lost &middot; '+legsPend+' pending</div>'
          +'</div>'
          +'<div style="text-align:right">'
            +'<div style="font-family:var(--orb);font-size:15px;font-weight:900;color:'+borderC+'">'+outcome.toUpperCase()+'</div>'
            +'<div style="font-family:var(--mono);font-size:12px;color:var(--gc);margin-top:2px">'+(parl.combinedDec||0).toFixed(2)+'x PAYOUT</div>'
          +'</div>'
        +'</div>'
        +'<div style="padding:0 10px 10px">'
          +'<div style="font-family:var(--mono);font-size:10px;color:var(--t3);letter-spacing:2px;margin:4px 0 6px">LEG BREAKDOWN</div>'
          +parl.legs.map(function(leg,i){
            var lo = leg.outcome||'pending';
            var lc = lo==='win'?'var(--gc)':lo==='loss'?'var(--hc)':'var(--t3)';
            var lml= leg.ml?(parseFloat(leg.ml)>0?'+':'')+leg.ml:'—';
            var legLeague = cvPickLeague({sport:leg.sport||leg.league||'',league:leg.league,hA:leg.betOn||''});
            return '<div style="display:flex;align-items:center;gap:6px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);flex-wrap:wrap">'
              +cvParlayLegTag(i+1)
              +cvLeagueTag(legLeague)
              +'<span style="font-family:var(--mono);font-size:10px;padding:1px 4px;border:1px solid rgba(255,255,255,.18);border-radius:2px;color:var(--t3)">'+(TYPE_LBL[leg.type||'ML']||leg.type||'ML')+'</span>'
              +'<span style="font-family:var(--mono);font-size:13px;color:var(--t);flex:1">'+leg.betOn+'</span>'
              +'<div style="display:flex;gap:6px;align-items:center;flex-shrink:0">'
                +'<span style="font-family:var(--mono);font-size:12px;color:var(--t2)">'+lml+'</span>'
                +'<span style="font-family:var(--mono);font-size:11px;color:var(--t3)">'+((leg.prob||.5)*100).toFixed(1)+'%</span>'
                +'<span style="font-family:var(--mono);font-size:10px;padding:1px 5px;border:1px solid '+lc+';color:'+lc+';border-radius:2px;min-width:48px;text-align:center">'+lo.toUpperCase()+'</span>'
              +'</div>'
            +'</div>';
          }).join('')
          +'<div style="display:flex;justify-content:space-between;font-family:var(--mono);font-size:11px;color:var(--t3);margin-top:6px;padding-top:5px;border-top:1px solid rgba(255,255,255,.08)">'
            +'<span>Combined prob: <strong style="color:var(--t)">'+(combP*100).toFixed(2)+'%</strong></span>'
            +'<span>Payout: <strong style="color:var(--gc)">'+(parl.combinedDec||0).toFixed(2)+'x</strong></span>'
            +'<span>EV: <strong style="color:'+(ev_parl>=0?'var(--nc)':'var(--hc)')+'">'+( ev_parl>=0?'+':'')+ev_parl.toFixed(1)+'%</strong></span>'
          +'</div>'
        +'</div>'
      +'</div>';
  });
  el.innerHTML=html;
}

function saveParlayToTracker(legs,sport){
  if(!legs||legs.length<2)return;
  var parlays=[];
  try{parlays=JSON.parse(localStorage.getItem('cv_parlays')||'[]');}catch(e){}
  var ml2d_=typeof ml2d==='function'?ml2d:function(m){var v=parseFloat(m);return v>0?v/100+1:100/Math.abs(v)+1;};
  var cD=legs.reduce(function(a,l){return a*ml2d_(l.ml||'-110');},1);
  var cP=legs.reduce(function(a,l){return a*(l.prob||.5);},1);
  var td=typeof today==='function'?today():new Date().toISOString().slice(0,10).replace(/-/g,'');
  parlays.unshift({id:'parl_'+Date.now(),date:td,sport:sport||'MULTI',league:sport||'MULTI',legs:legs,
    combinedDec:+cD.toFixed(4),combinedProb:+cP.toFixed(6),
    combinedML:Math.round(cD>2?(cD-1)*100:-(100/(cD-1))),
    outcome:'pending',lockedAt:Date.now()});
  if(parlays.length>50)parlays=parlays.slice(0,50);
  localStorage.setItem('cv_parlays',JSON.stringify(parlays));
  if(typeof toast==='function')toast('// PARLAY SAVED TO TRACKER');
}
</script>
"""


src_file = sys.argv[1] if len(sys.argv) > 1 else 'docs/app.html'
with open(src_file, encoding='utf-8') as f:
    html = f.read()

# Inject right after the SECOND </script> (main script close), before nav dropdowns
import re as _re
closes = [m.start() for m in _re.finditer(r'</script>', html)]
if len(closes) < 2:
    print("ERROR: can't find main </script>"); sys.exit(1)
main_close_end = closes[1] + len('</script>')
html = html[:main_close_end] + SIM_TRACKER_JS + html[main_close_end:]

with open(src_file, 'w', encoding='utf-8') as f:
    f.write(html)

print("Injected SIM+TRACKER script tag after main </script>")
print("Lines:", html.count('\n') + 1)
