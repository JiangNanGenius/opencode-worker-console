#pragma once
#include <ArduinoJson.h>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>
namespace board {
struct Disk {std::string name;float free=0,total=0,used=0;};
struct Task {std::string id,title,project,status;double started=0,ended=0,updated=0;std::string model;};
struct Source {std::string state="unavailable";double sampled=0;int total=0,active=0;std::vector<Task> items;};
struct Window {std::string label;float remaining=-1;double reset=0;float amount=0,limit=0;std::string unit;bool hasAmount=false;bool expiry=false;};
struct Balance {std::string currency;float remaining=0;};
// availability: 1 reported usable, 0 reported exhausted, -1 unknown (never faked).
// validUntil: producer-declared absolute epoch until which the row is authoritative
// (manual Ali snapshot has a 24h horizon); 0 = legacy row using the 15-minute age window.
struct Quota {std::string id,name,state="unavailable";double sampled=0,validUntil=0;std::vector<Window>windows;std::vector<Balance>balances;
 int availability=-1;bool stale=false;bool hasSnapshot=false;bool snapshotFresh=true;bool monthlyExhausted=false;};
struct Model {
 bool followMac=true;std::string ip,ssid,version;int utcOffset=28800;double now=0,systemSampled=0;std::string host;float cpu=0,mem=0,memUsed=0,memTotal=0,cpuTemp=NAN,gpuTemp=NAN,uptime=0;int fan=-1;
 std::vector<Disk>disks;Source codex,opencode;std::vector<Quota>quotas;bool wifi=false;
};
inline std::string str(JsonVariantConst v,size_t limit=288) {std::string s=v.is<const char*>()?v.as<const char*>():"";if(s.size()>limit){while(limit>0 && (uint8_t(s[limit])&0xc0)==0x80)--limit;s.resize(limit);}return s;}
inline Source parseSource(JsonVariantConst v) {
 Source s;s.state=str(v["state"],24);s.sampled=v["sampled_at"]|0.;s.total=v["total"]|0;s.active=v["active"]|s.total;
 for(JsonObjectConst t:v["items"].as<JsonArrayConst>()) {if(s.items.size()>=12)break;s.items.push_back({str(t["id"],64),str(t["title"]),str(t["project"],108),str(t["status"],24),t["started_at"]|0.,t["ended_at"]|0.,t["updated_at"]|0.,str(t["model"],96)});}
 return s;
}
inline bool parseModel(JsonVariantConst root,Model& m,double received) {
 int schema=root["schema_version"]|1;
 if(schema!=1 && schema!=2)return false;
 JsonVariantConst s=schema==2?root["system"]:root;
 if(!s["cpu"].is<float>() && !s["cpu"].is<int>())return false;
 Model n;n.followMac=m.followMac;n.ip=m.ip;n.ssid=m.ssid;n.version=m.version;n.utcOffset=m.utcOffset;if(root["utc_offset_seconds"].is<int>()){int offset=root["utc_offset_seconds"].as<int>();if(offset>=-50400&&offset<=50400)n.utcOffset=offset;}n.now=received;n.wifi=m.wifi;n.systemSampled=s["sampled_at"]|received;n.host=str(s["host"],64);
 n.cpu=s["cpu"]|0.f;n.mem=s["memory_percent"]|0.f;n.memUsed=s["memory_used_gb"]|0.f;n.memTotal=s["memory_total_gb"]|0.f;
 n.cpuTemp=s["cpu_temp_c"].is<float>()?s["cpu_temp_c"].as<float>():NAN;n.gpuTemp=s["gpu_temp_c"].is<float>()?s["gpu_temp_c"].as<float>():NAN;
 n.fan=s["fans"][0]["rpm"].is<int>()?s["fans"][0]["rpm"].as<int>():-1;n.uptime=s["uptime_hours"]|0.f;
 for(JsonObjectConst d:s["disks"].as<JsonArrayConst>()){if(n.disks.size()>=3)break;n.disks.push_back({str(d["name"],48),d["free_gb"]|0.f,d["total_gb"]|0.f,d["used_percent"]|0.f});}
  if(schema==2){n.codex=parseSource(root["tasks"]["codex"]);n.opencode=parseSource(root["tasks"]["opencode"]);
   // Quota providers are configured by the producer, in payload order. Nothing is
   // dropped for being unknown, stale or exhausted; that is displayed, not hidden.
   for(JsonObjectConst v:root["quotas"].as<JsonArrayConst>()) {if(n.quotas.size()>=8)break;Quota q;
    q.id=str(v["id"],32);q.name=str(v["name"],48);if(q.name.empty())q.name=q.id;
    q.state=str(v["state"],24);q.sampled=v["sampled_at"]|0.;
    double vu=v["valid_until"]|0.;if(std::isfinite(vu)&&vu>q.sampled)q.validUntil=vu;
    for(JsonObjectConst w:v["windows"].as<JsonArrayConst>()){if(q.windows.size()>=4)break;float r=w["remaining_percent"]|-1.f;
     if(std::isfinite(r)&&r>=0&&r<=100) {Window p{str(w["label"],24),r,w["resets_at"]|0.};
      if(p.label.empty())p.label=str(w["name"],24);
      if(p.reset<=0){double expires=w["expires_at"]|0.;if(std::isfinite(expires)&&expires>0){p.reset=expires;p.expiry=true;}}
      float a=w["remaining"]|-1.f,l=w["limit"]|-1.f;
      if(std::isfinite(a)&&std::isfinite(l)&&l>0){p.amount=a;p.limit=l;p.hasAmount=true;p.unit=str(w["unit"],16);}
      q.windows.push_back(std::move(p));}}
    for(JsonObjectConst b:v["balances"].as<JsonArrayConst>()){if(q.balances.size()>=2)break;q.balances.push_back({str(b["currency"],8),b["remaining"]|0.f});}
    JsonVariantConst av=v["available"];q.availability=av.is<bool>()?(av.as<bool>()?1:0):-1;
    q.stale=v["stale"].is<bool>()&&v["stale"].as<bool>();
    JsonObjectConst sp=v["snapshot"];if(!sp.isNull()){q.hasSnapshot=true;q.snapshotFresh=(sp["freshness"].is<const char*>()?std::string(sp["freshness"].as<const char*>())=="fresh":true)&&!(sp["expired"].is<bool>()&&sp["expired"].as<bool>());}
    JsonObjectConst bl=v["billing"];if(!bl.isNull())q.monthlyExhausted=bl["monthly_exhausted"].is<bool>()&&bl["monthly_exhausted"].as<bool>();
    n.quotas.push_back(std::move(q));
   }
  }
 m=std::move(n);return true;
}
inline bool sourceFresh(const Source&s,double now){return s.state=="ok"&&s.sampled>0&&now>=s.sampled-5&&now-s.sampled<=15;}
inline bool systemFresh(const Model&m){return m.systemSampled>0&&m.now>=m.systemSampled-5&&m.now-m.systemSampled<=15;}
// A row with a producer-declared valid_until stays fresh until that absolute epoch, so a
// configured manual snapshot (24h horizon) is not erased by the legacy 15-minute age limit.
// The bound also fails closed offline: an unrefreshed row expires and never stays fresh forever;
// 48h caps a mistyped far-future value. Rows without valid_until keep the 15-minute window.
inline bool quotaFresh(const Quota&q,double now){
 if(q.state!="ok"||q.sampled<=0||now<q.sampled-5||q.stale) return false;
 if(q.hasSnapshot&&!q.snapshotFresh) return false;
 if(q.validUntil>0){double until=q.validUntil<q.sampled+172800?q.validUntil:q.sampled+172800;return now<=until;}
 return now-q.sampled<=900;
}
// The Alibaba plan's credit pool is the amount-bearing window in "credits" units with
// the largest limit. Other units (afp) keep the incumbent window rows untouched.
inline int creditsWindow(const Quota&q){int best=-1;for(int i=0;i<int(q.windows.size());i++){const Window&w=q.windows[i];if(w.hasAmount&&w.unit=="credits"&&(best<0||w.limit>q.windows[best].limit))best=i;}return best;}
class Carousel {
public:
 int page=0,codexOffset=0,openOffset=0;bool paused=false;uint32_t began=0;
 static int quotaPages(const Model&m){return m.quotas.empty()?0:(int(m.quotas.size())+1)/2;}
 static int pageCount(const Model&m){return 3+quotaPages(m);}
 static uint32_t duration(int p){return p==0?18000:(p==1||p==2)?15000:8000;}
 bool included(int p,const Model&m)const {if(p==1)return !m.codex.items.empty()||m.codex.state!="ok";if(p==2)return !m.opencode.items.empty()||m.opencode.state!="ok";if(p>=3)return p-3<quotaPages(m);return true;}
 void next(const Model&m,uint32_t ms){int pc=pageCount(m),old=page;for(int i=0;i<pc;i++){page=(page+1)%pc;if(included(page,m))break;}
  if(old==1 && !m.codex.items.empty())codexOffset=(codexOffset+2)%(((m.codex.items.size()+1)/2)*2);
  if(old==2 && !m.opencode.items.empty())openOffset=(openOffset+2)%(((m.opencode.items.size()+1)/2)*2);
  began=ms;
 }
 void tick(const Model&m,uint32_t ms){int pc=pageCount(m);if(page>=pc){page=0;began=ms;return;}if(!included(page,m)||(!paused&&uint32_t(ms-began)>=duration(page)))next(m,ms);}
 void toggle(uint32_t ms){paused=!paused;began=ms;}
};
class Menu {
public:
 bool open=false,info=false,savedPaused=false;int selected=0,brightness=100;uint32_t activity=0;
 void close(Carousel&c,uint32_t ms){open=false;info=false;c.paused=savedPaused;c.began=ms;}
 // Bits: brightness=1, timezone=2, pause=4. Menu navigation returns zero.
 int action(int a,Model&m,Carousel&c,uint32_t ms){
  activity=ms;if(!open){if(a==2){open=true;info=false;selected=0;savedPaused=c.paused;c.paused=true;}else if(a==1)c.next(m,ms);return 0;}
  if(info){info=false;return 0;}
  if(a==1){selected=(selected+1)%5;return 0;}
  if(a!=2)return 0;
  if(selected==0){info=true;return 0;}
  if(selected==1){brightness=brightness>=100?25:brightness+25;return 1;}
  if(selected==2){savedPaused=!savedPaused;return 4;}
  if(selected==3){m.followMac=!m.followMac;return 2;}
  close(c,ms);return 0;
 }
 bool tick(Carousel&c,uint32_t ms){if(open&&uint32_t(ms-activity)>=60000){close(c,ms);return true;}return false;}
};
class Button {
 bool raw=false,down=false,longSent=false;uint32_t changed=0,pressed=0;
public:
 // 0 none, 1 short, 2 long. Unsigned subtraction tolerates millis rollover.
 int tick(bool high,uint32_t ms){if(high!=raw){raw=high;changed=ms;}if(raw!=down&&uint32_t(ms-changed)>=35){down=raw;if(down){pressed=ms;longSent=false;}else if(!longSent)return 1;}
  if(down&&!longSent&&uint32_t(ms-pressed)>=1000){longSent=true;return 2;}return 0;
 }
};
}
