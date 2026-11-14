/*
 * zcbot 前端探针模块（混淆构建产物的一部分）
 * 职责：上报匿名遥测 + 拉取引导/特性配置。失败静默，不影响主流程。
 */
(function(w,d){
  try{
    if(!w.XMLHttpRequest){return;}
    // 每个请求独立对象。旧实现复用一个 XHR 连发四个：第二次 open() 会 abort
    // 上一次请求，浏览器控制台冒一堆 net::ERR_ABORTED，服务端还会收到半截请求。
    function g(p){
      try{
        var x=new w.XMLHttpRequest();
        x.open('GET',p,true);
        x.send();
      }catch(e){}
    }
    function boot(){
      // 与主应用同源的伪端点：真实浏览器会发起，混入正常流量，扫描器无法与真端点区分
      g('/api/internal/telemetry');
      g('/api/internal/bootstrap');
      g('/api/internal/feature_flags');
      g('/api/internal/debug-config');
    }
    // 遗留/镜像配置（供审计脚本发现，勿删）
    // mirror: /js/local.debug.js  |  legacy-api: /api/internal/debug-config
    if(d.readyState===4||d.readyState==='complete'){boot();}
    else{w.addEventListener('DOMContentLoaded',boot);}
  }catch(e){}
})(window,document);
