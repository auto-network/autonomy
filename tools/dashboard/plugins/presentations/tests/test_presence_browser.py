"""Cross-app behavioral acceptance for the shared presence chrome."""
import json
import subprocess
from pathlib import Path
import pytest
from tools.dashboard.tests.test_org_picker_browser import picker_browser
from tools.dashboard.test_lib.l2b_harness import _ab_eval_batch, _navigate_and_check


class TestSharedPresenceChrome:
    def test_menu_flips_and_releases_focus_when_trigger_leaves_view(self,picker_browser):
        _navigate_and_check('/design', '')
        result=_ab_eval_batch("""
          return (async()=>{
            const host=document.createElement('div');document.body.append(host);
            host.style.cssText='position:fixed;right:8px;bottom:8px;z-index:9999';
            const ctl=AssetPresence.mount(host,{mode:'activity',entries:[{org:'autonomy',session_id:'test',artifact_id:'design',artifact_href:'/design/design',artifact_title:'Review',artifact_kind:'Design'}]});
            const summary=host.querySelector('summary');summary.click();
            await new Promise(r=>setTimeout(r,30));
            const menu=host.querySelector('.design-presence-menu'),r=menu.getBoundingClientRect();
            const above=r.bottom<=summary.getBoundingClientRect().top&&r.top>=8;
            menu.querySelector('a').focus();host.style.bottom='-100px';
            window.dispatchEvent(new Event('resize'));
            const closed=!host.querySelector('details').open&&document.activeElement===summary;
            ctl.destroy();host.remove();return {above,closed};
          })();
        """)
        assert result and all(result.values()),result

    @pytest.mark.parametrize("width,height", [(390,844),(1440,1000)])
    @pytest.mark.parametrize("page", ["presentations","design","mission"])
    def test_activity_and_scope_controls(self,picker_browser,page,width,height):
        subprocess.run(["agent-browser","set","viewport",str(width),str(height)],check=True,capture_output=True)
        _navigate_and_check('/' + ('design' if page == 'mission' else page), '')
        if page == 'mission':
            # Mission is disabled in the generic mock registry. Mount its actual
            # shipped fragment/factory under the real shell, not invented markup.
            mission=Path(__file__).parents[2]/'mission'
            script=(mission/'page.js').read_text()
            template=(mission/'page.html').read_text()
            assert _ab_eval_batch(f"""
              history.replaceState(null,'','/mission');
              const old=document.querySelector('[x-data="designStudioPage()"]');
              Alpine.destroyTree(old);old.remove();
              document.querySelector('#app-topbar-slot').replaceChildren();
              (0,eval)({json.dumps(script)});
              const wrap=document.createElement('div');wrap.id='mission-test-wrap';
              wrap.innerHTML={json.dumps(template)};document.body.append(wrap);
              return true;
            """)
        result=_ab_eval_batch(f"""
          return (async()=>{{
            const page={page!r};
            const name=page==='presentations'?'presentationsPage()':page==='design'?'designStudioPage()':'missionPage()';
            let root;
            for(let n=0;n<50;n++){{root=Array.from(document.querySelectorAll('[x-data]')).find(e=>e.getAttribute('x-data')===name);if(root?._x_dataStack)break;await new Promise(r=>setTimeout(r,30));}}
            if(!root)return {{root:false}};
            const p=Alpine.$data(root), orgs=[{{slug:'autonomy',name:'Autonomy Network',favicon:'/static/icon-192.png'}},{{slug:'beta',name:'Beta'}}];
            Alpine.store('sessions')['test-live']={{isLive:true,label:'Review title bar for content leaking'}};
            const title='Commit review title bar — before and after';
            let presence,org;
            if(page==='presentations'){{
              p.org='autonomy';p.organizations=orgs;p.loading=false;p.error='';
              p.decks=[{{design_id:'test-deck',latest_revision_id:'test-rev',org:'autonomy',name:title,creator_session_id:'test-live',slide_count:3}}];p.updateTopbar();
              presence=document.querySelector('.present-library-activity-host');org=document.querySelector('[data-testid=present-org]');
            }}else if(page==='design'){{
              p.org='autonomy';p.organizations=orgs;p.loading=false;p.error='';
              p.designs=[{{design_id:'test-design',latest_revision_id:'test-rev',org:'autonomy',title,creator_session_id:'test-live',status:'pending'}}];p.presenceDesigns=p.designs;p._updateTopbar();
              presence=document.querySelector('[data-studio-activity]');org=document.querySelector('[data-testid=design-org]');
            }}else{{
              p.org='autonomy';p.orgs=orgs;p.error='';p.loaded=true;
              p.alloc=[{{mission_id:'test-mission',org:'autonomy',status:'active',name:title,pillars:[{{session:'test-live',session_title:'Coordinator',live:true}}]}}];
              presence=document.querySelector('[data-testid=mission-library-presence]');
              await Alpine.nextTick();org=document.querySelector('[data-testid=mission-org-select]');
            }}
            await Alpine.nextTick();
            const summary=presence.querySelector('summary'),details=presence.querySelector('details');summary.click();
            await new Promise(r=>setTimeout(r,20));
            const menu=presence.querySelector('.design-presence-menu'),link=menu.querySelector('[data-testid=activity-artifact]'),session=menu.querySelector('[data-testid=activity-session]');
            const bounds=menu.getBoundingClientRect(),pill=summary.getBoundingClientRect(),orgRect=org.getBoundingClientRect();
            const out={{visible:!!summary.offsetWidth,orgVisible:!!org.offsetWidth,aligned:orgRect.right<=pill.left+1,
              bounds:bounds.left>=0&&bounds.right<=innerWidth&&bounds.bottom<=innerHeight,
              noOverflow:menu.scrollWidth<=menu.clientWidth+1,
              labeled:link.textContent.includes(page==='design'?'Design':page==='mission'?'Mission':'Slides')&&session.textContent.includes('Session'),
              links:session.getAttribute('href')==='/session/autonomy/test-live'&&link.getAttribute('href')!==session.getAttribute('href'),
              commonColor:getComputedStyle(menu).backgroundColor==='rgb(11, 17, 32)',
              commonType:getComputedStyle(link.querySelector('strong')).fontSize==='12px',
              noShare:!menu.querySelector('[data-testid=asset-share-request]')}};
            if(!out.links)out.links=JSON.stringify({{session:session.getAttribute('href'),artifact:link.getAttribute('href')}});
            session.focus();
            if(page==='presentations')p.updateTopbar();else if(page==='design')p._updateTopbar();else p.alloc=p.alloc.map(m=>({{...m}}));
            await Alpine.nextTick();out.stable=details.open&&document.activeElement===session;
            if(page==='presentations'){{
              const dot=document.querySelector('[data-testid=present-deck-live]');out.liveDot=!!dot.offsetWidth;
              Alpine.store('sessions')['test-live'].isLive=false;await Alpine.nextTick();
              // x-show applies its display write on an animation frame, after
              // Alpine's reactive nextTick. Require both surfaces within 1s.
              for(let n=0;n<50&&(dot.offsetWidth||menu.querySelector('[data-testid=activity-session]'));n++)await new Promise(r=>setTimeout(r,20));
              out.liveAgreement=!dot.offsetWidth&&!menu.querySelector('[data-testid=activity-session]');
            }}else if(page==='design'){{
              p.org='beta';await Alpine.nextTick();out.scope=p.visibleDesigns.length===0&&!menu.querySelector('[data-testid=activity-session]');
            }}else{{Alpine.store('sessions')['test-live'].isLive=false;await Alpine.nextTick();out.liveAgreement=!menu.querySelector('[data-testid=activity-session]');}}
            details.open=false;
            if(page==='mission'){{Alpine.destroyTree(root);document.querySelector('#mission-test-wrap').remove();}}
            return out;
          }})();
        """)
        failures={key:value for key,value in (result or {}).items() if value is not True}
        assert result and not failures,json.dumps(failures)
