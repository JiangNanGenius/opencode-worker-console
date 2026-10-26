#include "board_model.hpp"
#include <cassert>
#include <iostream>
static board::Quota mkQuota(const char*id,const char*name,double sampled=1000,const char*state="ok"){
 board::Quota q;q.id=id;q.name=name;q.state=state;q.sampled=sampled;return q;}
static void fillFour(board::Model&m){m.quotas={mkQuota("codex","Codex"),mkQuota("kimi","Kimi"),mkQuota("ark","火山方舟"),mkQuota("deepseek","DeepSeek")};}
int main(){
 board::Model m;m.codex.state="ok";m.opencode.state="ok";fillFour(m);board::Carousel c;
 assert(board::Carousel::pageCount(m)==5);assert(board::Carousel::quotaPages(m)==2);
 c.tick(m,17999);assert(c.page==0);c.tick(m,18000);assert(c.page==3);c.tick(m,26000);assert(c.page==4);c.tick(m,34000);assert(c.page==0);
 m.codex.items.resize(5);c.next(m,35000);assert(c.page==1);c.next(m,36000);assert(c.page==3&&c.codexOffset==2);
 c.page=1;c.next(m,40000);assert(c.codexOffset==4);c.page=1;c.next(m,45000);assert(c.codexOffset==0);
 c.toggle(45000);c.tick(m,100000);assert(c.page==3);c.toggle(100000);c.tick(m,107999);assert(c.page==3);
 c.began=0xfffffff0;c.tick(m,uint32_t(0xfffffff0u+8000));assert(c.page==4);
 // Five configured providers give three quota pages; the odd final slot is still a configured page.
 board::Model m5=m;m5.opencode.items.resize(2);m5.quotas.push_back(mkQuota("aliyun","阿里百炼"));board::Carousel c5;
 assert(board::Carousel::pageCount(m5)==6);
 int seen[6];for(int i=0;i<6;i++){seen[i]=c5.page;c5.next(m5,1000+i*8000);}
 assert(seen[0]==0&&seen[1]==1&&seen[2]==2&&seen[3]==3&&seen[4]==4&&seen[5]==5);
 assert(c5.page==0);c5.page=4;c5.tick(m5,100000);assert(c5.page==5);
 // Quota pages shrink and grow with configuration; an out-of-range page clamps without waiting for the timer.
 m5.quotas.pop_back();c5.tick(m5,107000);assert(c5.page==0);
 m5.quotas.clear();assert(board::Carousel::pageCount(m5)==3);
 c5.page=2;c5.next(m5,110000);assert(c5.page==0);
 // Durations preserved: system 18s, task pages 15s, every quota page 8s.
 assert(board::Carousel::duration(0)==18000&&board::Carousel::duration(1)==15000&&board::Carousel::duration(2)==15000);
 assert(board::Carousel::duration(3)==8000&&board::Carousel::duration(5)==8000);
 board::Menu menu;board::Carousel mc;board::Model mm;
 menu.action(2,mm,mc,100);assert(menu.open&&mc.paused);menu.action(2,mm,mc,200);assert(menu.info);menu.action(1,mm,mc,300);assert(!menu.info&&menu.open);
 menu.action(1,mm,mc,400);assert(menu.selected==1);assert(menu.action(2,mm,mc,500)==1&&menu.brightness==25);
 menu.action(1,mm,mc,600);assert(menu.action(2,mm,mc,700)==4&&menu.savedPaused);
 menu.action(1,mm,mc,800);assert(menu.action(2,mm,mc,900)==2&&!mm.followMac);
 menu.action(1,mm,mc,1000);menu.action(2,mm,mc,1100);assert(!menu.open&&mc.paused&&mc.began==1100);
 menu.action(2,mm,mc,1200);assert(!menu.tick(mc,61199));assert(menu.tick(mc,61200)&&!menu.open&&mc.paused);
 board::Button b;assert(b.tick(true,0)==0);assert(b.tick(false,10)==0);assert(b.tick(true,20)==0);assert(b.tick(true,60)==0);assert(b.tick(false,200)==0);assert(b.tick(false,240)==1);
 assert(b.tick(true,300)==0);assert(b.tick(true,340)==0);assert(b.tick(true,1340)==2);assert(b.tick(false,1400)==0);assert(b.tick(false,1440)==0);
 JsonDocument d;deserializeJson(d,R"({"cpu":10,"memory_percent":20,"host":"Legacy"})");assert(board::parseModel(d.as<JsonVariantConst>(),m,1800000000));assert(m.host=="Legacy");assert(board::systemFresh(m));m.now+=16;assert(!board::systemFresh(m));
 assert(m.utcOffset==28800);d["utc_offset_seconds"]=39600;assert(board::parseModel(d.as<JsonVariantConst>(),m,1800000000));assert(m.utcOffset==39600);d["utc_offset_seconds"]=-18000;assert(board::parseModel(d.as<JsonVariantConst>(),m,1800000000));assert(m.utcOffset==-18000);d["utc_offset_seconds"]=999999;assert(board::parseModel(d.as<JsonVariantConst>(),m,1800000000));assert(m.utcOffset==-18000);d.remove("utc_offset_seconds");assert(board::parseModel(d.as<JsonVariantConst>(),m,1800000000));assert(m.utcOffset==-18000);
 d["schema_version"]=99;assert(!board::parseModel(d.as<JsonVariantConst>(),m,1800000000));
 JsonDocument latest;deserializeJson(latest,R"({"schema_version":2,"system":{"cpu":1,"disks":[{"name":"Mac SSD"},{"name":"TECLAST"},{"name":"TECLAST-512"},{"name":"Overflow"}]},"tasks":{"opencode":{"state":"ok","items":[{"title":"Task","model":"ark-code-latest"}]}}})");assert(board::parseModel(latest.as<JsonVariantConst>(),m,1800000000));assert(m.disks.size()==3&&m.disks[2].name=="TECLAST-512");assert(m.opencode.items[0].model=="ark-code-latest");
 board::Quota q;q.state="ok";q.sampled=1000;assert(board::quotaFresh(q,1900));assert(!board::quotaFresh(q,1901));q.state="unavailable";assert(!board::quotaFresh(q,1001));
 // Dynamic quota parsing: payload order, name fallback, credits metadata and explicit states.
 JsonDocument five;deserializeJson(five,R"({"schema_version":2,"system":{"cpu":1},"quotas":[
  {"id":"codex","name":"Codex","state":"ok","sampled_at":1000,"windows":[{"label":"每周","remaining_percent":75,"resets_at":2000}],"balances":[]},
  {"id":"kimi","name":"Kimi","state":"ok","sampled_at":1000,"windows":[],"balances":[]},
  {"id":"ark","name":"火山方舟","state":"ok","sampled_at":1000,"windows":[],"balances":[],"stale":true},
  {"id":"deepseek","name":"DeepSeek","state":"ok","sampled_at":1000,"windows":[],"balances":[{"currency":"CNY","remaining":230.83}]},
  {"id":"aliyun","name":"阿里百炼","state":"ok","sampled_at":1000,"windows":[
    {"label":"每月","remaining_percent":99.9,"resets_at":1794823200,"remaining":99926.94,"limit":100000,"unit":"credits"},
    {"label":"5小时","remaining_percent":100,"resets_at":1100,"remaining":49000,"limit":49152,"unit":"credits"}],
   "balances":[],"available":true,"snapshot":{"captured_at":900,"freshness":"fresh","expired":false}},
  {"id":"ghost","state":"ok","sampled_at":1000,"windows":[],"balances":[]},
  {"id":"gone","state":"unavailable","sampled_at":0,"windows":[],"balances":[],"available":false},
  {"id":"overflow","name":" Eighth","state":"ok","sampled_at":1000,"windows":[],"balances":[]},
  {"id":"ninth","state":"ok","sampled_at":1000,"windows":[],"balances":[]}]})");
 board::Model pm;assert(board::parseModel(five.as<JsonVariantConst>(),pm,1100));
 assert(pm.quotas.size()==8);assert(pm.quotas[0].id=="codex"&&pm.quotas[4].id=="aliyun"&&pm.quotas[4].name=="阿里百炼");
 assert(pm.quotas[7].id=="overflow");
 assert(pm.quotas[5].name=="ghost");
 auto&a=pm.quotas[4];assert(a.availability==1&&!a.stale&&a.hasSnapshot&&a.snapshotFresh);
 assert(board::creditsWindow(a)==0);assert(a.windows[0].hasAmount&&a.windows[0].unit=="credits");
 assert(a.windows[0].amount>99926.8&&a.windows[0].amount<99927.0&&a.windows[0].limit==100000);
 assert(board::quotaFresh(a,1100));assert(board::Carousel::pageCount(pm)==7);
 assert(pm.quotas[2].stale&&!board::quotaFresh(pm.quotas[2],1100));  // producer staleness never looks real-time
 assert(pm.quotas[6].sampled==0&&pm.quotas[6].availability==0);       // exhausted unknown row is kept, never hidden
 board::Quota snap=a;snap.snapshotFresh=false;assert(!board::quotaFresh(snap,1100));
 board::Quota pool;pool.state="ok";pool.sampled=1000;pool.windows.push_back({"每周",50,2000,12,500,"afp",true});pool.windows.push_back({"每月",2.5,3000,12297,500000,"afp",true});
 assert(board::creditsWindow(pool)==-1);                                  // afp amounts keep the incumbent 4-window rows
 pool.windows[1].unit="credits";assert(board::creditsWindow(pool)==1);      // credits unit selects the largest pool
 JsonDocument billed;deserializeJson(billed,R"({"schema_version":2,"system":{"cpu":1},"quotas":[{"id":"ark","name":"火山方舟","state":"billing_blocked","sampled_at":1000,"windows":[],"balances":[],"billing":{"reason":"monthly_usage_limit","monthly_exhausted":true}}]})");
 board::Model bm;assert(board::parseModel(billed.as<JsonVariantConst>(),bm,1100));assert(bm.quotas[0].monthlyExhausted&&!bm.quotas[0].stale);
 // valid_until: the manual Ali snapshot keeps its configured 24h horizon, never the 15-minute erase,
 // but expires absolutely (offline can never keep a row fresh forever) and a far bound is capped at 48h.
 board::Quota manual=mkQuota("aliyun","阿里百炼",1000);manual.hasSnapshot=true;manual.validUntil=1000+24*3600;
 assert(board::quotaFresh(manual,1100));assert(board::quotaFresh(manual,1000+23*3600));
 assert(!board::quotaFresh(manual,1000+24*3600+1));
 JsonDocument cap;deserializeJson(cap,R"({"schema_version":2,"system":{"cpu":1},"quotas":[{"id":"aliyun","name":"阿里百炼","state":"ok","sampled_at":1000,"windows":[],"balances":[],"valid_until":4000000000}]})");
 board::Model cm;assert(board::parseModel(cap.as<JsonVariantConst>(),cm,1100));
 assert(cm.quotas[0].validUntil==4000000000);assert(board::quotaFresh(cm.quotas[0],1000+47*3600));assert(!board::quotaFresh(cm.quotas[0],1000+48*3600+1));
 board::Quota legacy=mkQuota("kimi","Kimi");assert(board::quotaFresh(legacy,1900));assert(!board::quotaFresh(legacy,1901));
 // Device /status emission: dynamic ids must serialize (an unbound variant does not auto-attach children).
 {JsonDocument out;JsonObject quot=out["quotas"].to<JsonObject>();
  for(auto&qu:pm.quotas){JsonObject item=quot[qu.id].to<JsonObject>();item["fresh"]=board::quotaFresh(qu,1100);
   int ci=board::creditsWindow(qu);if(ci>=0){auto&w=qu.windows[ci];JsonObject cr=item["credits"].to<JsonObject>();cr["unit"]=w.unit;}}
  std::string s;serializeJson(out,s);
  assert(s.find("\"codex\"")!=std::string::npos&&s.find("\"aliyun\"")!=std::string::npos);
  assert(s.find("\"credits\":{")!=std::string::npos&&s.find("\"unit\":\"credits\"")!=std::string::npos);}
 JsonDocument badVu;deserializeJson(badVu,R"({"schema_version":2,"system":{"cpu":1},"quotas":[{"id":"kimi","name":"Kimi","state":"ok","sampled_at":5000,"windows":[],"balances":[],"valid_until":4000}]})");
 board::Model vm;assert(board::parseModel(badVu.as<JsonVariantConst>(),vm,5100));assert(vm.quotas[0].validUntil==0&&board::quotaFresh(vm.quotas[0],5100));
 // Expiry is an absolute plan boundary, distinct from a reset. Prefer a real
 // reset if a producer supplies both, and keep positional Window initializers valid.
 JsonDocument expiry;deserializeJson(expiry,R"({"schema_version":2,"system":{"cpu":1},"quotas":[{"id":"kimi","state":"ok","sampled_at":1000,"windows":[{"label":"套餐","remaining_percent":96,"expires_at":3000},{"label":"每周","remaining_percent":75,"resets_at":2000,"expires_at":3000}]}]})");
 board::Model em;assert(board::parseModel(expiry.as<JsonVariantConst>(),em,1100));
 assert(em.quotas[0].windows[0].expiry&&em.quotas[0].windows[0].reset==3000);
 assert(!em.quotas[0].windows[1].expiry&&em.quotas[0].windows[1].reset==2000);
 board::Window oldInitializer{"每周",50,2000,12,500,"afp",true};assert(!oldInitializer.expiry);
 std::cout<<"carousel, dynamic pagination, pause, rollover, debounce, compatibility, freshness, credits, valid_until, expiry passed\n";
}
