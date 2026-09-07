// Approved Design Studio revision 594c1737-5e8b-434c-ab09-84f31d8ea497.
// The DOM below is the approved artifact materialized as a production screen;
// only its fixture arrays were replaced by the API-backed view state here.
(function () {
  'use strict';
  var api = window.AutonomyOrgSettings;
  if (!api) return;
  var lastCount = 0;

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function icon(kind) {
    if (kind === 'share') return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V3M7 8l5-5 5 5"/><path d="M5 12v8h14v-8"/></svg>';
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 5h5v5M19 5l-9 9"/><path d="M19 13v6H5V5h6"/></svg>';
  }
  function request(url, options) {
    options = options || {};
    options.headers = Object.assign({'Accept':'application/json'}, options.headers || {});
    return fetch(url, options).then(function (res) {
      if (res.status === 204) return null;
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) { var err = new Error(body.error || ('HTTP ' + res.status)); err.detail = body.detail || ''; throw err; }
        return body;
      });
    });
  }
  function relativeExpiry(seconds) {
    if (seconds == null) return '';
    var left = seconds * 1000 - Date.now();
    if (left <= 0) return 'Expired';
    var hours = Math.ceil(left / 3600000);
    return hours < 48 ? 'Expires in ' + hours + ' hour' + (hours === 1 ? '' : 's')
      : 'Expires in ' + Math.ceil(hours / 24) + ' days';
  }
  function shareUrl(title, url, button) {
    if (navigator.share) return navigator.share({title:title, url:url}).catch(function (e) {
      if (!e || e.name !== 'AbortError') throw e;
    });
    return navigator.clipboard.writeText(url).then(function () {
      button.classList.add('copied');
      setTimeout(function () { button.classList.remove('copied'); }, 1600);
    });
  }

  function Controller(slug, root, data) {
    this.slug = slug; this.root = root; this.open = null; this.error = ''; this.errorDetail = '';
    this.tab = 'services'; this.shareType = 'note'; this.zoneKind = 'parent-txt';
    this.absorb(data);
  }
  Controller.prototype.absorb = function (data) {
    this.services = data.services || [];
    this.shares = (data.shares || []).filter(function (s) { return !s.expired; });
    this.serviceWarning = data.service_warning || '';
    this.zones = data.zones || [];
    this.personaDomain = data.persona_domain || '';
    this.orgUuid = data.org_uuid || '';
  };
  Controller.prototype.fail = function (e) {
    this.error = e && e.message ? e.message : String(e);
    this.errorDetail = e && e.detail ? e.detail : '';
    this.render();
  };
  // Publishing places: the persona apex first, then every claimed zone.
  Controller.prototype.domainOptions = function () {
    var options = [];
    if (this.personaDomain) options.push({value: '', label: this.personaDomain});
    this.zones.forEach(function (z) { options.push({value: z.zone, label: z.zone}); });
    return options;
  };
  Controller.prototype.zoneRecords = function (zone, kind) {
    zone = (zone || '<zone>').toLowerCase();
    var parent = zone.indexOf('.') >= 0 ? zone.slice(zone.indexOf('.') + 1) : '<parent>';
    var org = this.orgUuid || '<organization id>';
    if (kind === 'ns-token') return [zone + '  NS  ' + org + '.ns.auto.network'];
    return [zone + '  NS  ns1.auto.network', zone + '  NS  ns2.auto.network', '_autonomy.' + parent + '  TXT  "autonomy-org=' + org + '"'];
  };
  Controller.prototype.render = function () {
    var self = this;
    lastCount = this.services.length + this.shares.length;
    var live = this.services.filter(function (s) { return s.state === 'active'; }).length;
    var shareTypes = [['note','Notes'],['design','Designs'],['present','Presents'],['mission','Missions']];
    var body = '<p class="pl-intro">Services and shared artifacts published by this organization.</p>' +
      (this.error ? '<p class="pl-error">'+esc(this.error)+(this.errorDetail?' <span style="color:#9ca3af">— '+esc(this.errorDetail)+'</span>':'')+'</p>' : '') +
      '<div class="pl-tabs" role="tablist"><button data-tab="services" class="'+(this.tab==='services'?'on':'')+'">Services <sup class="pl-tabcount">'+live+'</sup></button><button data-tab="shares" class="'+(this.tab==='shares'?'on':'')+'">Shares <sup class="pl-tabcount">'+this.shares.length+'</sup></button></div>';
    if (this.tab === 'services') {
      body += '<div data-panel="services">';
      if (this.serviceWarning && this.services.length) body += '<div class="pl-notice"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/></svg><span>'+esc(this.serviceWarning)+'</span></div>';
      this.services.forEach(function (service) { body += self.serviceCard(service); });
      if (!this.services.length) body += '<article class="pl-empty"><div class="pl-empty-title">No Services published</div><p>Instantly publish any local service to the Internet by simply asking your agent. All connections are end-to-end encrypted directly to your machine. You have no Services currently published.</p></article>';
      body += this.zonesPanel();
      body += '</div>';
    } else {
      body += '<div class="pl-share-tabs" role="tablist">';
      shareTypes.forEach(function (pair) {
        var count = self.shares.filter(function (s) { return s.type === pair[0]; }).length;
        body += '<button data-share-tab="'+pair[0]+'" class="'+(self.shareType===pair[0]?'on':'')+'">'+pair[1]+' <sup>'+count+'</sup></button>';
      });
      body += '</div>';
      var shown = this.shares.filter(function (s) { return s.type === self.shareType; });
      shown.forEach(function (share) { body += self.shareCard(share); });
      if (!shown.length) {
        var labels = {note:['Notes','Share a Graph Note with anyone using a secure link. You have no Notes currently published.'],design:['Designs','Share a Design Studio design for review or collaboration. You have no Designs currently published.'],present:['Presents','Share a presentation as a secure link for anyone you choose. You have no Presents currently published.'],mission:['Missions','Share a Mission Control view with collaborators or stakeholders. You have no Missions currently published.']};
        body += '<article class="pl-empty"><div class="pl-empty-title">No '+labels[this.shareType][0]+' published</div><p>'+labels[this.shareType][1]+'</p></article>';
      }
    }
    this.root.innerHTML = body;
    this.bind();
  };
  Controller.prototype.zonesPanel = function () {
    var self = this, open = this.open === 'zone:add';
    var body = '<div class="pl-zones"><div class="pl-eyebrow" style="margin:18px 0 6px">Custom domains</div>';
    this.zones.forEach(function (z) {
      var confirm = self.open === 'zone-release:' + z.zone;
      body += '<article class="pl-card" data-zone="'+esc(z.zone)+'"><div class="pl-row"><span class="pl-state"><i class="pl-dot"></i>Verified</span><div class="pl-hosted" style="margin-top:0"><div class="pl-eyebrow">Services publish under</div><div class="pl-hostline"><span class="pl-host-app">*</span><span class="pl-host-dot">.</span><span class="pl-host-domain">'+esc(z.zone)+'</span></div></div><div class="pl-footer"><span class="pl-terminal">'+esc(z.binding_kind)+'</span><span></span><div class="pl-pair"><button class="pl-danger" data-action="release">Release</button></div><span></span><span></span></div></div>'+
        '<div class="pl-detail '+(confirm?'open':'')+'"><div class="pl-share-title">Release this domain?</div><p style="margin:5px 0 0;color:#9ca3af;font-size:12px">Stop every Service published under it first. The delegation at your DNS provider is left in place.</p><div class="pl-formactions"><button class="pl-secondary" data-action="cancel">Cancel</button><button class="pl-danger" data-action="confirm-release">Release domain</button></div></div></article>';
    });
    body += '<article class="pl-card" data-zone-add><div class="pl-row"><div class="pl-subtitle">Publish Services directly under a domain you own, as <b>app.your-zone</b>. Delegate the zone to Autonomy at your DNS provider, then verify it here.</div><div class="pl-footer" style="margin-top:10px"><button class="pl-secondary" data-action="zone-add">'+(this.zones.length?'Add another domain':'Add a custom domain')+'</button><span></span><span></span><span></span><span></span></div></div>'+
      '<div class="pl-detail '+(open?'open':'')+'"><div class="pl-field"><label>Zone (a subdomain you delegate, e.g. autonomy.example.com)</label><input data-field="zone" placeholder="autonomy.example.com" value="'+esc(this.zoneDraft||'')+'"></div><div class="pl-field" style="margin-top:10px"><label>Binding</label><select data-field="kind"><option value="parent-txt" '+(this.zoneKind==='parent-txt'?'selected':'')+'>TXT in the parent zone (standard)</option><option value="ns-token" '+(this.zoneKind==='ns-token'?'selected':'')+'>Per-organization name server</option></select></div><div class="pl-field" style="margin-top:10px"><label>Records to create at your DNS provider</label><pre class="pl-terminal" style="margin:0;white-space:pre-wrap;padding:8px;background:#0b1220;border:1px solid #1f2937;border-radius:6px">'+esc(this.zoneRecords(this.zoneDraft, this.zoneKind).join('\n'))+'</pre></div><div class="pl-formactions"><button class="pl-secondary" data-action="cancel">Cancel</button><button class="pl-primary" data-action="zone-claim">Verify and claim</button></div></div></article></div>';
    return body;
  };
  Controller.prototype.serviceCard = function (s) {
    var host = String(s.origin || '').replace(/^https:\/\//,'');
    var app = s.app_label, domain = s.zone || s.persona_label, root = s.zone ? '' : '.serve.auto.network';
    var short = host.length <= 38; var detail = this.open === 'rename:'+s.reservation_id ? 'rename' : (this.open === 'stop:'+s.reservation_id ? 'stop' : '');
    var options = this.domainOptions(), current = s.zone || '';
    if (!options.some(function (o) { return o.value === current; })) options.unshift({value: current, label: domain + root});
    var select = options.map(function (o) { return '<option value="'+esc(o.value)+'" '+(o.value===current?'selected':'')+'>'+esc(o.label)+'</option>'; }).join('');
    return '<article class="pl-card" data-service="'+esc(s.reservation_id)+'"><div class="pl-row"><span class="pl-state '+(s.state==='paused'?'paused':'')+'"><i class="pl-dot"></i>'+(s.state==='paused'?'Paused':'Live')+'</span><div class="pl-session"><div class="pl-eyebrow">Hosted by</div><div class="pl-session-title">'+esc(s.session_title)+'</div><div class="pl-terminal">'+esc(s.target && s.target.session_id || 'Target unavailable')+'</div></div><div class="pl-hosted"><div class="pl-eyebrow">Hosted at</div><div class="pl-hostline"><span class="pl-host-app">'+esc(app)+'</span><span class="pl-host-dot">.</span><span class="pl-host-domain">'+esc(domain)+'</span>'+(root?'<span class="pl-host-root '+(short?'inline':'')+'">'+root+'</span>':'')+'</div></div><div class="pl-footer"><button class="pl-secondary" data-action="rename">Rename</button><span></span><div class="pl-pair"><button class="pl-secondary" data-action="toggle">'+(s.state==='paused'?'Resume':'Pause')+'</button><button class="pl-danger" data-action="stop">Stop</button></div><span></span><div class="pl-icons"><button class="pl-icon" data-action="share" aria-label="Share service">'+icon('share')+'</button><button class="pl-icon" data-action="visit" aria-label="Open service">'+icon('open')+'</button></div></div></div>'+
      '<div class="pl-detail '+(detail==='rename'?'open':'')+'"><div class="pl-field"><label>Service hostname</label><input data-field="app" value="'+esc(s.app_label)+'"></div><div class="pl-field" style="margin-top:10px"><label>Publish under</label><select data-field="domain">'+select+'</select></div><p style="margin:8px 0 0;color:#fbbf24;font-size:11px">Changing either field creates a new public address. The current address stops after the new one is live.</p><div class="pl-formactions"><button class="pl-secondary" data-action="cancel">Cancel</button><button class="pl-primary" data-action="save">Save address</button></div></div>'+
      '<div class="pl-detail '+(detail==='stop'?'open':'')+'"><div class="pl-share-title">Stop this Service?</div><p style="margin:5px 0 0;color:#9ca3af;font-size:12px">This closes the public connection. The current address will stop working.</p><div class="pl-formactions"><button class="pl-secondary" data-action="cancel">Cancel</button><button class="pl-danger" data-action="confirm-stop">Stop Service</button></div></div></article>';
  };
  Controller.prototype.shareCard = function (s) {
    var detail = this.open === 'revoke:'+s.token;
    return '<article class="pl-card" data-share="'+esc(s.token)+'"><div class="pl-row"><div><div class="pl-share-title">'+esc(s.title)+'</div><div class="pl-subtitle">'+esc(s.description)+'</div>'+(s.expires_at?'<div class="pl-expiry">'+esc(relativeExpiry(s.expires_at))+'</div>':'')+'</div><div class="pl-share-footer"><button class="pl-secondary" data-action="view">View</button><span></span><div class="pl-pair">'+(s.expires_at?'<button class="pl-secondary" data-action="extend">Extend</button>':'')+'<button class="pl-danger" data-action="revoke">Revoke</button></div><span></span><div class="pl-icons"><button class="pl-icon" data-action="share" aria-label="Share link">'+icon('share')+'</button><button class="pl-icon" data-action="visit" aria-label="Open public link">'+icon('open')+'</button></div></div></div><div class="pl-detail '+(detail?'open':'')+'"><div class="pl-share-title">Revoke this Share?</div><p style="margin:5px 0 0;color:#9ca3af;font-size:12px">Anyone using this link will immediately lose access. This cannot be undone.</p><div class="pl-formactions"><button class="pl-secondary" data-action="cancel">Cancel</button><button class="pl-danger" data-action="confirm-revoke">Revoke Share</button></div></div></article>';
  };
  Controller.prototype.bind = function () {
    var self = this;
    this.root.querySelectorAll('[data-tab]').forEach(function (b) { b.onclick=function(){self.tab=b.dataset.tab;self.render();}; });
    this.root.querySelectorAll('[data-share-tab]').forEach(function (b) { b.onclick=function(){self.shareType=b.dataset.shareTab;self.render();}; });
    this.root.querySelectorAll('[data-service]').forEach(function (card) {
      var s=self.services.find(function(x){return x.reservation_id===card.dataset.service;});
      card.onclick=function(e){var a=e.target.closest('[data-action]');if(!a)return;self.serviceAction(a.dataset.action,s,card,a);};
    });
    this.root.querySelectorAll('[data-share]').forEach(function (card) {
      var s=self.shares.find(function(x){return x.token===card.dataset.share;});
      card.onclick=function(e){var a=e.target.closest('[data-action]');if(!a)return;self.shareAction(a.dataset.action,s,a);};
    });
    this.root.querySelectorAll('[data-zone]').forEach(function (card) {
      var z=self.zones.find(function(x){return x.zone===card.dataset.zone;});
      card.onclick=function(e){var a=e.target.closest('[data-action]');if(!a)return;self.zoneAction(a.dataset.action,z,card);};
    });
    var add=this.root.querySelector('[data-zone-add]');
    if(add){
      add.onclick=function(e){var a=e.target.closest('[data-action]');if(!a)return;self.zoneAction(a.dataset.action,null,add);};
      var zoneInput=add.querySelector('[data-field="zone"]'), kind=add.querySelector('[data-field="kind"]');
      if(zoneInput) zoneInput.oninput=function(){self.zoneDraft=zoneInput.value.trim();var pre=add.querySelector('pre');if(pre)pre.textContent=self.zoneRecords(self.zoneDraft,self.zoneKind).join('\n');};
      if(kind) kind.onchange=function(){self.zoneKind=kind.value;var pre=add.querySelector('pre');if(pre)pre.textContent=self.zoneRecords(self.zoneDraft,self.zoneKind).join('\n');};
    }
  };
  Controller.prototype.zoneAction = function (action, z, card) {
    var self=this;
    if(action==='cancel'){this.open=null;return this.render();}
    if(action==='zone-add'){this.open='zone:add';return this.render();}
    if(action==='release'){this.open='zone-release:'+z.zone;return this.render();}
    if(action==='confirm-release'){
      return request('/api/network/serve-zones/'+encodeURIComponent(z.zone),{method:'DELETE',headers:{'X-Graph-Org':this.slug}}).then(function(){return self.refresh();}).catch(function(e){self.fail(e);});
    }
    if(action==='zone-claim'){
      var zone=card.querySelector('[data-field="zone"]').value.trim(); if(!zone)return;
      this.zoneDraft=zone; card.classList.add('pl-busy');
      return request('/api/network/serve-zones',{method:'POST',headers:{'Content-Type':'application/json','X-Graph-Org':this.slug},body:JSON.stringify({zone:zone,binding_kind:this.zoneKind})}).then(function(){self.zoneDraft='';return self.refresh();}).catch(function(e){card.classList.remove('pl-busy');self.fail(e);});
    }
  };
  Controller.prototype.refresh = function () { var self=this; return request('/api/network/published-links',{headers:{'X-Graph-Org':this.slug}}).then(function(d){self.absorb(d);self.open=null;self.error='';self.errorDetail='';self.render();}); };
  Controller.prototype.serviceAction = function (action,s,card,button) {
    var self=this, url=s.origin;
    if(action==='share') return shareUrl(s.app_label,url,button).catch(function(e){self.fail(e);});
    if(action==='visit') return window.open(url,'_blank','noopener');
    if(action==='cancel'){this.open=null;return this.render();}
    if(action==='rename'){this.open='rename:'+s.reservation_id;return this.render();}
    if(action==='stop'){this.open='stop:'+s.reservation_id;return this.render();}
    if(action==='toggle') return this.transition(s,s.state==='paused'?'active':'paused');
    if(action==='confirm-stop') return this.transition(s,'released');
    if(action==='save') {
      var app=card.querySelector('[data-field="app"]').value.trim(); if(!app)return;
      var zone=card.querySelector('[data-field="domain"]').value; var reserve={app_label:app}; if(zone) reserve.zone=zone;
      if(app===s.app_label && zone===(s.zone||'')){this.open=null;return this.render();}
      var oldTarget=s.target;
      request('/api/network/service-reservations',{method:'POST',headers:{'Content-Type':'application/json','X-Graph-Org':this.slug},body:JSON.stringify(reserve)}).then(function(r){
        if(!oldTarget)return r;
        return request('/api/network/service-targets/'+encodeURIComponent(r.reservation.reservation_id),{method:'PUT',headers:{'Content-Type':'application/json','X-Graph-Org':self.slug},body:JSON.stringify({session_id:oldTarget.session_id,port:oldTarget.port})}).then(function(){return r;});
      }).then(function(){return self.transition(s,'released');}).catch(function(e){self.fail(e);});
    }
  };
  Controller.prototype.transition = function(s,state){var self=this;return request('/api/network/service-reservations/'+encodeURIComponent(s.reservation_id)+'/state',{method:'PUT',headers:{'Content-Type':'application/json','X-Graph-Org':this.slug},body:JSON.stringify({state:state})}).then(function(){return self.refresh();}).catch(function(e){self.fail(e);});};
  Controller.prototype.shareAction = function(action,s,button){var self=this;if(action==='view'){api.close();return window.navigateTo?window.navigateTo(s.platform_url):window.location.assign(s.platform_url);}if(action==='share')return shareUrl(s.title,s.url,button).catch(function(e){self.fail(e);});if(action==='visit')return window.open(s.url,'_blank','noopener');if(action==='cancel'){this.open=null;return this.render();}if(action==='revoke'){this.open='revoke:'+s.token;return this.render();}if(action==='extend'){this.error='This link’s signed expiry cannot be changed in place yet.';return this.render();}if(action==='confirm-revoke'){return request('/api/approvals',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'link_revoke',session:'dashboard-ui',request:{org:this.slug,token:s.token}})}).then(function(r){self.open=null;self.render();if(window.openApprovalOverlay)return window.openApprovalOverlay(r.id);}).catch(function(e){self.fail(e);});}};

  api.register({id:'published-links',label:'Published Links',windowTitle:'Published Services & Links',order:20,render:function(slug,opts){var root=document.createElement('section');root.className='published-links';root.innerHTML='<div class="orgset-loading">Loading…</div>';return request('/api/network/published-links',{headers:{'X-Graph-Org':slug}}).then(function(data){var controller=new Controller(slug,root,data);var focus=opts&&opts.focus;var share=focus&&controller.shares.find(function(s){return s.token===focus;});if(share){controller.tab='shares';controller.shareType=share.type;}controller.render();return root;});},count:function(){return lastCount?{text:lastCount,tone:'ready'}:null;}});
})();
