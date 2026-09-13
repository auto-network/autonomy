// Approved identity design 8492c7d3: production IO for its existing editor state.
(function () {
  'use strict';
  async function request(url, options) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!response.ok || body.ok === false) throw new Error(body.error || 'The profile could not be saved.');
    return body;
  }
  window.personalProfile = function () {
    return {
      view: 'settings', pTab: 'personal', organizations: [], hasProfile: false,
      name: '', bio: '', initialsOverride: '', photo: null, userName: '', userInitial: '',
      dirty: false, saveState: 'idle', nameError: '', saved: null,
      photoStage: 'none', photoError: '', uploadPct: 0, pendingPhoto: null,
      pendW: 0, pendH: 0, zoom: 1, offsetX: 0, offsetY: 0,
      async init() {
        try {
          const {profile} = await request('/api/identity/profile');
          if (!profile) throw new Error('Create your personal identity before setting up your profile.');
          this.restore(profile);
        } catch (error) { this.nameError = error.message; }
        try {
          const body = await request('/api/orgs');
          this.organizations = (body.orgs || []).map(entry => {
            const org = entry.org || {}, identity = entry.identity_resolved || {};
            return {slug: org.slug, name: identity.name || org.name || org.slug,
              initials: identity.initial || (identity.name || org.slug || '').slice(0, 2)};
          }).filter(org => org.slug && org.slug !== 'personal');
        } catch (error) { this.nameError = error.message; }
      },
      restore(profile) {
        this.saved = profile;
        this.name = profile.display_name || '';
        this.bio = profile.biography || '';
        this.initialsOverride = profile.initials_override || '';
        this.photo = profile.avatar_url || null;
        this.hasProfile = !!profile.persisted;
        this.dirty = false;
      },
      openOrganization(slug) { window.AutonomyOrgSettings.open(slug); },
      get trimmedName() { return this.name.trim(); },
      get avatarName() { return this.trimmedName || this.userName; },
      get derivedInitials() { return this.trimmedName.split(/\s+/).filter(Boolean).slice(0, 2).map(word => Array.from(word)[0]).join('').toUpperCase(); },
      get profileInitials() { return this.initialsOverride.trim() || this.derivedInitials; },
      get creating() { return !this.hasProfile; },
      get hasPhoto() { return !!this.photo; },
      get photoSrc() { return this.photo; },
      get pendingSrc() { return this.pendingPhoto; },
      get altText() { return 'Profile photo of ' + this.avatarName; },
      get _cover() { return Math.max(256 / this.pendW, 256 / this.pendH); },
      get _scale() { return this._cover * this.zoom; },
      get cropStyle() { return this.imageStyle(1); },
      get miniStyle() { return this.imageStyle(48 / 256); },
      imageStyle(f) {
        return 'width:' + (this.pendW * this._scale * f) + 'px;height:' + (this.pendH * this._scale * f) + 'px;transform:translate(' + (this.offsetX * f) + 'px,' + (this.offsetY * f) + 'px);';
      },
      _clamp() {
        this.offsetX = Math.max(256 - this.pendW * this._scale, Math.min(0, this.offsetX));
        this.offsetY = Math.max(256 - this.pendH * this._scale, Math.min(0, this.offsetY));
      },
      startDrag(e) { this._drag = {x: e.clientX, y: e.clientY, ox: this.offsetX, oy: this.offsetY}; e.target.setPointerCapture(e.pointerId); },
      onDrag(e) {
        if (!this._drag) return;
        this.offsetX = this._drag.ox + e.clientX - this._drag.x;
        this.offsetY = this._drag.oy + e.clientY - this._drag.y;
        this._clamp();
      },
      endDrag() { this._drag = null; },
      onZoom() {
        // Zoom around the centre of the viewport, not the image's top-left
        // corner: keep the source point under the centre where it was.
        const prev = this._zoomScale || this._scale;
        const next = this._scale;
        if (prev && next && prev !== next) {
          this.offsetX = 128 - (128 - this.offsetX) * next / prev;
          this.offsetY = 128 - (128 - this.offsetY) * next / prev;
        }
        this._zoomScale = next;
        this._clamp();
      },
      addPhoto() { this.photoError = ''; this.pickFile(); },
      pickFile() { this.$refs.file.value = ''; this.$refs.file.click(); },
      async onFile(e) {
        const file = e.target.files && e.target.files[0];
        if (!file) return;
        try {
          if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('Choose a JPEG, PNG, or WebP photo.');
          if (file.size > 10 * 1024 * 1024) throw new Error('That photo exceeds the 10 MiB limit. Choose a smaller photo.');
          const img = new Image();
          const url = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = () => reject(new Error('That photo could not be read. Choose another photo.'));
            reader.readAsDataURL(file);
          });
          try {
            img.src = url;
            await img.decode();
            if (img.naturalWidth < 128 || img.naturalHeight < 128) throw new Error('Choose a photo at least 128 × 128 px.');
            if (img.naturalWidth * img.naturalHeight > 40000000) throw new Error('Choose a photo with no more than 40 million pixels.');
          } catch (error) { throw error; }
          this.releasePending();
          this.pendingPhoto = url;
          this.pendW = img.naturalWidth; this.pendH = img.naturalHeight;
          this.zoom = 1; this._zoomScale = this._scale;
          this.offsetX = (256 - this.pendW * this._scale) / 2; this.offsetY = (256 - this.pendH * this._scale) / 2; this._clamp();
          this.photoStage = 'cropping';
        } catch (error) { this.photoError = error.message; this.photoStage = 'error'; }
      },
      async applyCrop() {
        this.photoStage = 'uploading'; this.uploadPct = 0;
        try {
          // The approved editor exports precisely its square viewport. The
          // existing processor owns canonical WebP encoding and storage.
          const img = new Image(); img.src = this.pendingSrc; await img.decode();
          const canvas = document.createElement('canvas'); canvas.width = canvas.height = 512;
          canvas.getContext('2d').drawImage(img, -this.offsetX / this._scale, -this.offsetY / this._scale,
            256 / this._scale, 256 / this._scale, 0, 0, 512, 512);
          const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
          if (!blob) throw new Error('The selected crop could not be read. Choose another photo.');
          const form = new FormData(); form.append('avatar', blob, 'profile.png');
          form.append('crop', JSON.stringify({x: 0, y: 0, size: 1}));
          const result = await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/identity/profile/avatar');
            xhr.upload.onprogress = event => { if (event.lengthComputable) this.uploadPct = Math.round(event.loaded / event.total * 100); };
            xhr.onload = () => {
              try {
                const body = JSON.parse(xhr.responseText);
                if (xhr.status < 200 || xhr.status >= 300 || !body.ok) throw new Error(body.error || 'The photo could not be stored.');
                resolve(body);
              } catch (error) { reject(error); }
            };
            xhr.onerror = () => reject(new Error('The photo could not be uploaded. Check your connection and try again.'));
            xhr.send(form);
          });
          this.photo = result.avatar_url;
          this.hasProfile = true;
          if (this.saved) Object.assign(this.saved, result, {persisted: true});
          this.cancelPhoto(); this.changed();
        } catch (error) { this.photoError = error.message; this.photoStage = 'error'; }
      },
      releasePending() { this.pendingPhoto = null; },
      destroy() { this.releasePending(); },
      cancelPhoto() { this.photoStage = 'none'; this.releasePending(); this.photoError = ''; },
      askRemove() { this.photoStage = 'removeConfirm'; },
      async confirmRemove() {
        this.photoStage = 'uploading'; this.uploadPct = 0;
        try {
          await request('/api/identity/profile/avatar', {method: 'DELETE'});
          this.photo = null;
          if (this.saved) Object.assign(this.saved, {avatar_url: null, avatar_attachment_id: null});
          this.cancelPhoto(); this.changed();
        } catch (error) { this.photoError = error.message; this.photoStage = 'error'; }
      },
      changed() { window.dispatchEvent(new CustomEvent('autonomy:identity-changed')); },
      onEdit() { this.dirty = true; this.saveState = 'idle'; this.nameError = ''; },
      async save() {
        if (!this.trimmedName) { this.nameError = 'Add a display name so people can recognize you.'; return; }
        this.nameError = ''; this.saveState = 'saving';
        try {
          const {profile} = await request('/api/identity/profile', {method: 'PATCH', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({display_name: this.trimmedName, biography: this.bio, initials: this.initialsOverride.trim()})});
          this.restore(profile); this.saveState = 'saved'; this.changed();
        } catch (error) { this.nameError = error.message; this.saveState = 'idle'; }
      },
      discard() { if (this.saved) this.restore(this.saved); this.nameError = ''; this.saveState = 'idle'; }
    };
  };
})();
