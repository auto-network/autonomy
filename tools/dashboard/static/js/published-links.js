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
    this.tab = 'services'; this.shareType = 'note';
    this.domainFilter = ''; this.zoneStatus = null; this.zoneDraft = '';
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
  // One set of records: the delegation's NS names carry the organization id
  // (<org>.ns1/ns2.auto.network), so the same records both hand the name to
  // Autonomy and bind it to this organization — no separate TXT.
  Controller.prototype.zoneRecords = function (zone) {
    zone = (zone || 'autonomy.example.com').toLowerCase();
    var parent = zone.indexOf('.') >= 0 ? zone.slice(zone.indexOf('.') + 1) : 'example.com';
    var org = this.orgUuid || '<organization id>';
    return {
      zone: zone, parent: parent,
      delegation: [{name: zone, type: 'NS', value: org + '.ns1.auto.network'}, {name: zone, type: 'NS', value: org + '.ns2.auto.network'}]
    };
  };
  // The registry's refusal, in words a DNS-provider user can act on.
  Controller.prototype.zoneReason = function (e) {
    var code = e && e.message || '', detail = e && e.detail || '';
    if (code === 'zone_unverified') {
      if (/does not delegate/.test(detail)) return 'Your DNS provider is not pointing this name at the two Autonomy name servers below yet, or points it at other servers too. DNS changes can take a few minutes to appear; try again shortly.';
      if (/lookup failed/.test(detail)) return 'The name could not be looked up yet. Check the spelling and try again in a minute.';
      return 'Not verified yet: ' + detail;
    }
    if (code === 'zone_owned_elsewhere') return 'This domain is already connected to another organization.';
    if (code === 'zone_invalid') return detail || 'That is not a valid domain name.';
    if (code === 'serving_unavailable') return 'This dashboard is not connected to the network right now, so the domain cannot be verified.';
    if (code === 'zone_in_use') return 'Stop the services published under this domain first.';
    return detail ? code + ': ' + detail : code;
  };
  function recordRows(records) {
    return records.map(function (r) {
      return '<div class="pl-record"><div class="pl-record-cell"><span class="pl-record-k">Name</span><code>'+esc(r.name)+'</code></div><div class="pl-record-cell pl-record-type"><span class="pl-record-k">Type</span><code>'+esc(r.type)+'</code></div><div class="pl-record-cell"><span class="pl-record-k">Value</span><code>'+esc(r.value)+'</code><button class="pl-copy" data-copy="'+esc(r.value)+'" aria-label="Copy value">Copy</button></div></div>';
    }).join('');
  }
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
      var shownServices = this.domainFilter ? this.services.filter(function (s) { return s.zone === self.domainFilter; }) : this.services;
      if (this.domainFilter) body += '<div class="pl-filter">Showing services under <b>'+esc(this.domainFilter)+'</b> <button data-action="clear-filter">Show all</button></div>';
      shownServices.forEach(function (service) { body += self.serviceCard(service); });
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
      var count = self.services.filter(function (s) { return s.zone === z.zone; }).length;
      body += '<article class="pl-card" data-zone="'+esc(z.zone)+'"><div class="pl-row"><span class="pl-state"><i class="pl-dot"></i>Verified</span><div class="pl-hosted" style="margin-top:0"><div class="pl-eyebrow">Services publish under</div><div class="pl-hostline"><span class="pl-host-app">*</span><span class="pl-host-dot">.</span><span class="pl-host-domain">'+esc(z.zone)+'</span></div></div><div class="pl-zone-meta"><button class="pl-link" data-action="filter">'+count+' service'+(count===1?'':'s')+'</button><span class="pl-zone-note">Any member of this organization can publish here.</span></div><div class="pl-footer pl-footer-end"><button class="pl-danger" data-action="release">Release</button></div></div>'+
        '<div class="pl-detail '+(confirm?'open':'')+'"><div class="pl-share-title">Release this domain?</div><p style="margin:5px 0 0;color:#9ca3af;font-size:12px">'+(count?'Stop the '+count+' service'+(count===1?'':'s')+' published under it first. ':'')+'Services can no longer be published under it until it is verified again. The records at your DNS provider are left in place.</p><div class="pl-formactions"><button class="pl-secondary" data-action="cancel">Cancel</button><button class="pl-danger" data-action="confirm-release">Release domain</button></div></div></article>';
    });
    var rec = this.zoneRecords(this.zoneDraft), preview = this.zoneDraft ? 'service.' + this.zoneDraft.toLowerCase() : 'service.autonomy.example.com';
    var st = this.zoneStatus, statusHtml = '';
    if (st && st.kind === 'busy') statusHtml = '<div class="pl-status busy">Verifying…</div>';
    else if (st && st.kind === 'error') statusHtml = '<div class="pl-status error">'+esc(st.text)+'</div>';
    else if (st && st.kind === 'ok') statusHtml = '<div class="pl-status ok">✓ Verified — '+esc(st.text)+' is connected to this organization.</div>';
    body += '<article class="pl-card" data-zone-add><div class="pl-row"><div class="pl-subtitle">Publish services directly under a subdomain you control, as <b>app.autonomy.yourdomain.com</b>.</div><div class="pl-footer" style="margin-top:10px"><button class="pl-secondary" data-action="zone-add">Add custom domain</button></div></div>'+
      '<div class="pl-detail '+(open?'open':'')+'">'+
        '<div class="pl-field"><label>Your domain</label><input data-field="zone" placeholder="autonomy.example.com" value="'+esc(this.zoneDraft||'')+'"><div class="pl-hint">A subdomain you control, for example autonomy.example.com. Support for a whole domain such as example.com is coming.</div><div class="pl-hint" data-role="preview">Services you publish here will appear as <b>'+esc(preview)+'</b>.</div></div>'+
        '<div class="pl-step"><div class="pl-step-title">Name server delegation</div><p class="pl-step-text">Tell your DNS provider that Autonomy answers for <b data-role="zone">'+esc(rec.zone)+'</b>: in the zone for <b data-role="parent">'+esc(rec.parent)+'</b>, add these two NS records. The name servers carry your organization id, so the same records also prove the name is yours; no other record is needed. Nothing else under <span data-role="zone">'+esc(rec.zone)+'</span> will resolve from your provider after this, which is why a dedicated subdomain is used.</p><div data-role="delegation">'+recordRows(rec.delegation)+'</div></div>'+
        '<div data-role="status">'+statusHtml+'</div>'+
        '<div class="pl-formactions"><button class="pl-secondary" data-action="cancel">Cancel</button><button class="pl-primary" data-action="zone-claim">Verify</button></div></div></article></div>';
    return body;
  };
  Controller.prototype.serviceCard = function (s) {
    var host = String(s.origin || '').replace(/^https:\/\//,'');
    var app = s.app_label, domain = s.zone || s.persona_label, root = s.zone ? '' : '.serve.auto.network';
    var short = host.length <= 38; var detail = this.open === 'rename:'+s.reservation_id ? 'rename' : (this.open === 'stop:'+s.reservation_id ? 'stop' : '');
    var options = this.domainOptions(), current = s.zone || '';
    if (!options.some(function (o) { return o.value === current; })) options.unshift({value: current, label: domain + root});
    var select = options.map(function (o) { return '<option value="'+esc(o.value)+'" '+(o.value===current?'selected':'')+'>'+esc(o.label)+'</option>'; }).join('');
    return '<article class="pl-card" data-service="'+esc(s.reservation_id)+'"><div class="pl-row"><span class="pl-state '+(s.state==='paused'?'paused':'')+'"><i class="pl-dot"></i>'+(s.state==='paused'?'Paused':'Live')+'</span><div class="pl-session"><div class="pl-eyebrow">Hosted by</div><div class="pl-session-title">'+esc(s.session_title)+'</div><div class="pl-terminal">'+esc(s.target && s.target.session_id || 'Target unavailable')+'</div>'+(s.publisher?'<div class="pl-publisher">Published by <b>'+esc(s.publisher.display_name)+'</b>'+(s.session_local===false?' · on another member\'s machine':'')+'</div>':'')+'</div><div class="pl-hosted"><div class="pl-eyebrow">Hosted at</div><div class="pl-hostline"><span class="pl-host-app">'+esc(app)+'</span><span class="pl-host-dot">.</span><span class="pl-host-domain">'+esc(domain)+'</span>'+(root?'<span class="pl-host-root '+(short?'inline':'')+'">'+root+'</span>':'')+'</div></div><div class="pl-footer"><button class="pl-secondary" data-action="rename">Rename</button><span></span><div class="pl-pair"><button class="pl-secondary" data-action="toggle">'+(s.state==='paused'?'Resume':'Pause')+'</button><button class="pl-danger" data-action="stop">Stop</button></div><span></span><div class="pl-icons"><button class="pl-icon" data-action="share" aria-label="Share service">'+icon('share')+'</button><button class="pl-icon" data-action="visit" aria-label="Open service">'+icon('open')+'</button></div></div></div>'+
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
      var zoneInput=add.querySelector('[data-field="zone"]');
      if(zoneInput) zoneInput.oninput=function(){
        self.zoneDraft=zoneInput.value.trim(); self.zoneStatus=null;
        var rec=self.zoneRecords(self.zoneDraft);
        add.querySelector('[data-role="preview"]').innerHTML='Services you publish here will appear as <b>'+esc('service.'+(self.zoneDraft||'autonomy.example.com').toLowerCase())+'</b>.';
        add.querySelectorAll('[data-role="zone"]').forEach(function(n){n.textContent=rec.zone;});
        add.querySelectorAll('[data-role="parent"]').forEach(function(n){n.textContent=rec.parent;});
        add.querySelector('[data-role="delegation"]').innerHTML=recordRows(rec.delegation);
        add.querySelector('[data-role="status"]').innerHTML='';
      };
    }
    this.root.querySelectorAll('[data-copy]').forEach(function (b) {
      b.onclick=function(e){e.stopPropagation();navigator.clipboard.writeText(b.dataset.copy).then(function(){b.textContent='Copied';setTimeout(function(){b.textContent='Copy';},1400);});};
    });
    var clear=this.root.querySelector('[data-action="clear-filter"]');
    if(clear) clear.onclick=function(){self.domainFilter='';self.render();};
  };
  Controller.prototype.zoneAction = function (action, z, card) {
    var self=this;
    if(action==='cancel'){this.open=null;this.zoneStatus=null;return this.render();}
    if(action==='zone-add'){this.open='zone:add';this.zoneStatus=null;return this.render();}
    if(action==='filter'){this.domainFilter=z.zone;return this.render();}
    if(action==='release'){this.open='zone-release:'+z.zone;return this.render();}
    if(action==='confirm-release'){
      return request('/api/network/serve-zones/'+encodeURIComponent(z.zone),{method:'DELETE',headers:{'X-Graph-Org':this.slug}}).then(function(){return self.refresh();}).catch(function(e){self.error=self.zoneReason(e);self.errorDetail='';self.render();});
    }
    if(action==='zone-claim'){
      var zone=card.querySelector('[data-field="zone"]').value.trim(); if(!zone)return;
      this.zoneDraft=zone; this.zoneStatus={kind:'busy'}; this.render();
      return request('/api/network/serve-zones',{method:'POST',headers:{'Content-Type':'application/json','X-Graph-Org':this.slug},body:JSON.stringify({zone:zone,binding_kind:'ns-token'})}).then(function(r){
        self.zoneStatus={kind:'ok',text:(r&&r.zone&&r.zone.zone)||zone};
        return request('/api/network/published-links',{headers:{'X-Graph-Org':self.slug}}).then(function(d){self.absorb(d);self.error='';self.errorDetail='';self.render();});
      }).catch(function(e){self.zoneStatus={kind:'error',text:self.zoneReason(e)};self.render();});
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
