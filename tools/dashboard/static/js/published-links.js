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
        if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
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
    this.slug = slug; this.root = root; this.services = data.services || [];
    this.shares = (data.shares || []).filter(function (s) { return !s.expired; });
    this.serviceWarning = data.service_warning || '';
    this.tab = 'services'; this.shareType = 'note'; this.open = null; this.error = '';
  }
  Controller.prototype.render = function () {
    var self = this;
    lastCount = this.services.length + this.shares.length;
    var live = this.services.filter(function (s) { return s.state === 'active'; }).length;
    var shareTypes = [['note','Notes'],['design','Designs'],['present','Presents'],['mission','Missions']];
    var body = '<p class="pl-intro">Services and shared artifacts published by this organization.</p>' +
      (this.error ? '<p class="pl-error">'+esc(this.error)+'</p>' : '') +
      '<div class="pl-tabs" role="tablist"><button data-tab="services" class="'+(this.tab==='services'?'on':'')+'">Services <sup class="pl-tabcount">'+live+'</sup></button><button data-tab="shares" class="'+(this.tab==='shares'?'on':'')+'">Shares <sup class="pl-tabcount">'+this.shares.length+'</sup></button></div>';
    if (this.tab === 'services') {
      body += '<div data-panel="services">';
      if (this.serviceWarning && this.services.length) body += '<div class="pl-notice"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/></svg><span>'+esc(this.serviceWarning)+'</span></div>';
      this.services.forEach(function (service) { body += self.serviceCard(service); });
      if (!this.services.length) body += '<article class="pl-empty"><div class="pl-empty-title">No Services published</div><p>Instantly publish any local service to the Internet by simply asking your agent. All connections are end-to-end encrypted directly to your machine. You have no Services currently published.</p></article>';
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
  Controller.prototype.serviceCard = function (s) {
    var host = String(s.origin || '').replace(/^https:\/\//,'');
    var bits = host.split('.'); var app = bits.shift() || s.app_label; var domain = bits.slice(0,-3).join('.') || s.persona_label;
    var short = host.length <= 38; var detail = this.open === 'rename:'+s.reservation_id ? 'rename' : (this.open === 'stop:'+s.reservation_id ? 'stop' : '');
    return '<article class="pl-card" data-service="'+esc(s.reservation_id)+'"><div class="pl-row"><span class="pl-state '+(s.state==='paused'?'paused':'')+'"><i class="pl-dot"></i>'+(s.state==='paused'?'Paused':'Live')+'</span><div class="pl-session"><div class="pl-eyebrow">Hosted by</div><div class="pl-session-title">'+esc(s.session_title)+'</div><div class="pl-terminal">'+esc(s.target && s.target.session_id || 'Target unavailable')+'</div></div><div class="pl-hosted"><div class="pl-eyebrow">Hosted at</div><div class="pl-hostline"><span class="pl-host-app">'+esc(app)+'</span><span class="pl-host-dot">.</span><span class="pl-host-domain">'+esc(domain)+'</span><span class="pl-host-root '+(short?'inline':'')+'">.serve.auto.network</span></div></div><div class="pl-footer"><button class="pl-secondary" data-action="rename">Rename</button><span></span><div class="pl-pair"><button class="pl-secondary" data-action="toggle">'+(s.state==='paused'?'Resume':'Pause')+'</button><button class="pl-danger" data-action="stop">Stop</button></div><span></span><div class="pl-icons"><button class="pl-icon" data-action="share" aria-label="Share service">'+icon('share')+'</button><button class="pl-icon" data-action="visit" aria-label="Open service">'+icon('open')+'</button></div></div></div>'+
      '<div class="pl-detail '+(detail==='rename'?'open':'')+'"><div class="pl-field"><label>Service hostname</label><input data-field="app" value="'+esc(s.app_label)+'"></div><div class="pl-field" style="margin-top:10px"><label>Publish under</label><select data-field="domain"><option value="'+esc(s.persona_label)+'.serve.auto.network">'+esc(s.persona_label)+'</option></select></div><p style="margin:8px 0 0;color:#fbbf24;font-size:11px">Changing either field creates a new public address. The current address stops after the new one is live.</p><div class="pl-formactions"><button class="pl-secondary" data-action="cancel">Cancel</button><button class="pl-primary" data-action="save">Save address</button></div></div>'+
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
  };
  Controller.prototype.refresh = function () { var self=this; return request('/api/network/published-links',{headers:{'X-Graph-Org':this.slug}}).then(function(d){self.services=d.services||[];self.shares=(d.shares||[]).filter(function(s){return !s.expired;});self.serviceWarning=d.service_warning||'';self.open=null;self.error='';self.render();}); };
  Controller.prototype.serviceAction = function (action,s,card,button) {
    var self=this, url=s.origin;
    if(action==='share') return shareUrl(s.app_label,url,button).catch(function(e){self.error=e.message;self.render();});
    if(action==='visit') return window.open(url,'_blank','noopener');
    if(action==='cancel'){this.open=null;return this.render();}
    if(action==='rename'){this.open='rename:'+s.reservation_id;return this.render();}
    if(action==='stop'){this.open='stop:'+s.reservation_id;return this.render();}
    if(action==='toggle') return this.transition(s,s.state==='paused'?'active':'paused');
    if(action==='confirm-stop') return this.transition(s,'released');
    if(action==='save') {
      var app=card.querySelector('[data-field="app"]').value.trim(); if(!app)return;
      var oldTarget=s.target;
      request('/api/network/service-reservations',{method:'POST',headers:{'Content-Type':'application/json','X-Graph-Org':this.slug},body:JSON.stringify({app_label:app})}).then(function(r){
        if(!oldTarget)return r;
        return request('/api/network/service-targets/'+encodeURIComponent(r.reservation.reservation_id),{method:'PUT',headers:{'Content-Type':'application/json','X-Graph-Org':self.slug},body:JSON.stringify({session_id:oldTarget.session_id,port:oldTarget.port})}).then(function(){return r;});
      }).then(function(){return self.transition(s,'released');}).catch(function(e){self.error=e.message;self.render();});
    }
  };
  Controller.prototype.transition = function(s,state){var self=this;return request('/api/network/service-reservations/'+encodeURIComponent(s.reservation_id)+'/state',{method:'PUT',headers:{'Content-Type':'application/json','X-Graph-Org':this.slug},body:JSON.stringify({state:state})}).then(function(){return self.refresh();}).catch(function(e){self.error=e.message;self.render();});};
  Controller.prototype.shareAction = function(action,s,button){var self=this;if(action==='view'){api.close();return window.navigateTo?window.navigateTo(s.platform_url):window.location.assign(s.platform_url);}if(action==='share')return shareUrl(s.title,s.url,button).catch(function(e){self.error=e.message;self.render();});if(action==='visit')return window.open(s.url,'_blank','noopener');if(action==='cancel'){this.open=null;return this.render();}if(action==='revoke'){this.open='revoke:'+s.token;return this.render();}if(action==='extend'){this.error='This link’s signed expiry cannot be changed in place yet.';return this.render();}if(action==='confirm-revoke'){return request('/api/approvals',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'link_revoke',session:'dashboard-ui',request:{org:this.slug,token:s.token}})}).then(function(r){self.open=null;self.render();if(window.openApprovalOverlay)return window.openApprovalOverlay(r.id);}).catch(function(e){self.error=e.message;self.render();});}};

  api.register({id:'published-links',label:'Published Links',windowTitle:'Published Services & Links',order:20,render:function(slug,opts){var root=document.createElement('section');root.className='published-links';root.innerHTML='<div class="orgset-loading">Loading…</div>';return request('/api/network/published-links',{headers:{'X-Graph-Org':slug}}).then(function(data){var controller=new Controller(slug,root,data);var focus=opts&&opts.focus;var share=focus&&controller.shares.find(function(s){return s.token===focus;});if(share){controller.tab='shares';controller.shareType=share.type;}controller.render();return root;});},count:function(){return lastCount?{text:lastCount,tone:'ready'}:null;}});
})();
