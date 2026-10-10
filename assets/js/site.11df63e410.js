/* AscendPoint Agency — site behaviour (menu, filters). */
(function(){
var h=document.querySelector('.site-header'),t=document.querySelector('.menu-toggle');
if(t){t.addEventListener('click',function(){var o=h.classList.toggle('open');t.setAttribute('aria-expanded',o);t.setAttribute('aria-label',o?'Close menu':'Open menu');});}
document.querySelectorAll('.nav a').forEach(function(a){a.addEventListener('click',function(){h.classList.remove('open');if(t)t.setAttribute('aria-expanded','false');});});
var fb=document.querySelectorAll('.filters button');
fb.forEach(function(b){b.addEventListener('click',function(){fb.forEach(function(x){x.setAttribute('aria-pressed',x===b)});var f=b.dataset.f;document.querySelectorAll('[data-brand]').forEach(function(c){c.hidden=!(f==='all'||c.dataset.brand===f)});});});
var form=document.getElementById('contact-form');
if(form){
 var who=document.getElementById('f-who');
 try{var p=location.hash.replace('#','');if(p==='founder'&&who)who.value='Agency founder';}catch(e){}
 form.addEventListener('submit',function(e){e.preventDefault();var ok=true;
  form.querySelectorAll('[required]').forEach(function(el){var m=document.getElementById(el.id+'-err');var bad=!el.value.trim()||(el.type==='email'&&!/^\S+@\S+\.\S+$/.test(el.value));if(m)m.hidden=!bad;el.setAttribute('aria-invalid',bad);if(bad&&ok){el.focus();ok=false;}});
  if(!ok)return;var n=document.getElementById('f-name').value.trim().split(' ')[0];
  document.getElementById('thanks-name').textContent=n?(', '+n):'';
  form.hidden=true;var th=document.getElementById('thanks');th.hidden=false;th.focus();});
 var again=document.getElementById('again');if(again)again.addEventListener('click',function(){form.reset();form.hidden=false;document.getElementById('thanks').hidden=true;});
}
/* Time-limited items (banner, training cards) hide themselves after their data-hide-after time. */
var now=Date.now();
document.querySelectorAll('[data-hide-after]').forEach(function(e){if(now>=Date.parse(e.getAttribute('data-hide-after')))e.hidden=true;});
document.querySelectorAll('[data-hide-if-empty]').forEach(function(s){if(!s.querySelector('[data-hide-after]:not([hidden])'))s.hidden=true;});
/* About timeline: entries start light and darken as they scroll into view. */
var tl=document.querySelector('.tl');
if(tl&&'IntersectionObserver' in window&&!matchMedia('(prefers-reduced-motion: reduce)').matches){
 tl.classList.add('tl-anim');
 var io=new IntersectionObserver(function(es){es.forEach(function(e){e.target.classList.toggle('in',e.isIntersecting||e.boundingClientRect.top<0);});},{rootMargin:'0px 0px -30% 0px'});
 tl.querySelectorAll('.tl-item').forEach(function(i){io.observe(i);});
}
})();
