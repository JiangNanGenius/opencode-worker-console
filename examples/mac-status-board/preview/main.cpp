#include <LovyanGFX.hpp>
#include <LGFX_AUTODETECT.hpp>
#include "board_ui.hpp"
#include <fstream>
#include <sstream>
#include <filesystem>
#include <cassert>
#include <chrono>
LGFX screen(320,480);
std::string inputPath,outputPath;
static std::vector<uint16_t> pixels(){std::vector<uint16_t>p(320*480);screen.readRect(0,0,320,480,p.data());return p;}
void save(const std::string&name){std::vector<uint8_t>pixels(320*480*3);screen.readRectRGB(0,0,320,480,pixels.data());std::ofstream f(outputPath+"/"+name+".ppm",std::ios::binary);f<<"P6\n320 480\n255\n";f.write(reinterpret_cast<char*>(pixels.data()),pixels.size());}
// Page images follow the firmware carousel: system, Codex, OpenCode, then one image
// per dynamically configured quota page (two providers per page, odd last slot blank).
static std::vector<std::string> pageNames(const board::Model&m){
 std::vector<std::string> names;char b[32];
 int pc=board::Carousel::pageCount(m);
 for(int p=0;p<pc;p++){
  if(p==0)names.push_back("01-system");
  else if(p==1)names.push_back("02-codex");
  else if(p==2)names.push_back("03-opencode");
  else{snprintf(b,sizeof(b),"%02d-quota-%d",p+1,p-2);names.push_back(b);}
 }
 return names;
}
static int quotaPageOf(const board::Model&m,const std::string&id){
 for(int i=0;i<int(m.quotas.size());i++)if(m.quotas[i].id==id)return 3+i/2;
 return -1;
}
int run(bool*running){
 screen.init();board::Renderer renderer(screen);if(!renderer.begin())return 2;
 std::ifstream file(inputPath);std::stringstream buffer;buffer<<file.rdbuf();JsonDocument doc;
 if(deserializeJson(doc,buffer.str()))return 3;
 board::Model model;if(!board::parseModel(doc.as<JsonVariantConst>(),model,doc["unix_time"]|0.))return 4;
 std::string all=buffer.str();std::ifstream uiFile("include/board_ui.hpp");std::stringstream uiText;uiText<<uiFile.rdbuf();all+=uiText.str();std::vector<uint16_t>missing;uint16_t code=0;int pending=0;
 for(uint8_t ch:all){if(ch<128){code=ch;pending=0;}else if((ch&0xe0)==0xc0){code=ch&31;pending=1;continue;}else if((ch&0xf0)==0xe0){code=ch&15;pending=2;continue;}else if((ch&0xc0)==0x80&&pending){code=(code<<6)|(ch&63);if(--pending)continue;}else continue;
 if(code>=32){lgfx::FontMetrics metric;fonts::efontCN_16.getDefaultMetric(&metric);if(!fonts::efontCN_16.updateFontMetric(&metric,code)&&std::find(missing.begin(),missing.end(),code)==missing.end())missing.push_back(code);}}
 std::ofstream coverage(outputPath+"/font-coverage.txt");coverage<<"Missing glyph count: "<<missing.size()<<"\n";for(auto ch:missing)coverage<<std::hex<<ch<<"\n";coverage.close();
 model.wifi=true;board::Carousel c;auto names=pageNames(model);
 int pc=int(names.size());
 for(int p=0;p<pc;p++){c.page=p;renderer.render(model,c,4000);save(names[p]);}
 for(int p=0;p<pc;p++){c.page=p;renderer.render(model,c,4000);renderer.renderProgress(c,4500);save("progress-fast-"+std::to_string(p));renderer.render(model,c,4500);save("progress-full-"+std::to_string(p));renderer.renderProgress(c,1000);save("progress-reset-fast-"+std::to_string(p));renderer.render(model,c,1000);save("progress-reset-full-"+std::to_string(p));}
 // Compare actual panel pixels: the fast edge must exactly match a full paint
 // for advance, reset, pause, page transitions and millis rollover.
 std::ofstream checks(outputPath+"/renderer-checks.txt");
 for(int p=0;p<pc;p++){
  c.page=p;c.paused=false;c.began=0;renderer.render(model,c,0);
  int writes=0;for(uint32_t ms=16;ms<=1008;ms+=16)writes+=renderer.renderProgress(c,ms);
  assert(writes>=58);checks<<"page "<<p<<" progress writes / 1008ms: "<<writes<<"\n";
  for(uint32_t ms:{1009u,1025u,1057u,4001u,3999u,0u,18000u}){
   renderer.renderProgress(c,ms);auto fast=pixels();renderer.render(model,c,ms);assert(fast==pixels());
  }
  renderer.render(model,c,0);c.paused=true;assert(renderer.renderProgress(c,1234));auto paused=pixels();renderer.render(model,c,1234);assert(paused==pixels());assert(!renderer.renderProgress(c,1250));
  c.paused=false;c.began=0xfffffff0u;renderer.render(model,c,c.began);renderer.renderProgress(c,uint32_t(c.began+1025u));auto rolled=pixels();renderer.render(model,c,uint32_t(c.began+1025u));assert(rolled==pixels());
 }
 c={};renderer.render(model,c,4000);auto original=pixels();
 auto caller=model;assert(renderer.startFrame(caller,c,4000));assert(!renderer.startFrame(model,c,6000));
 auto changed=model;changed.host="MUTATED AFTER SNAPSHOT";changed.cpu=99;changed.codex.items.clear();
 // Mutating the caller's object must never alter an active frame's later stripes.
 while(renderer.frameActive()){caller=changed;renderer.renderStep();}assert(original==pixels());
 assert(!renderer.renderStep());
 assert(renderer.startFrame(model,c,4000));int steps=0;while(renderer.frameActive()){renderer.renderStep();++steps;renderer.renderProgress(c,4000+steps*16);}assert(steps==12);
 auto interleaved=pixels();renderer.render(model,c,4000+steps*16);assert(interleaved==pixels());
 checks<<"AA advance/reset/pause/rollover pixels, frozen snapshot, busy guard, 12 incremental stripes, interleaved progress: passed\n";
 board::Window exp{"套餐",96,model.now+86400*30};exp.expiry=true;assert(board::windowTime(exp,model.now)=="30.0天到期");assert(board::windowTime(exp,exp.reset)=="已到期");
 checks.close();
 c.began=0;c.paused=false;
 std::ofstream timings(outputPath+"/renderer-timing-us.csv");timings<<"page,iteration,frame_us,max_step_us\n";
 for(int p=0;p<pc;p++)for(int iteration=0;iteration<4;iteration++){
  c.page=p;auto begin=std::chrono::steady_clock::now();renderer.startFrame(model,c,4000);long long longest=0;
  while(renderer.frameActive()){auto step=std::chrono::steady_clock::now();renderer.renderStep();longest=std::max(longest,std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now()-step).count());}
  timings<<p<<","<<iteration<<","<<std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now()-begin).count()<<","<<longest<<"\n";
 }
 timings.close();
 board::Renderer fullFrame(screen,480);fullFrame.begin();for(int p=0;p<pc;p++){c.page=p;renderer.render(model,c,4000);auto strips=pixels();fullFrame.render(model,c,4000);assert(strips==pixels());save(std::string("diagnostic-full-")+names[p]);}
 auto fixture=model;c.page=1;fixture.codex.state="ok";fixture.codex.sampled=fixture.now;
 fixture.codex.total=3;fixture.codex.items={{"long","这是一项用于检查中文长标题的测试任务：恢复设备并验证所有页面的显示效果以及更长的标题应该正确省略而不是越过屏幕边缘","测试场景 · 不是真实任务","running",fixture.now-4380,0,fixture.now},{"wait","确认升级结果并处理下一步工作","测试场景 · 标题换行","attention",fixture.now-360,0,fixture.now}};
 renderer.render(fixture,c,7000);save("06-long-title-fixture");
 fixture=model;fixture.systemSampled=fixture.now-75;c.page=0;renderer.render(fixture,c,6000);save("07-offline-fixture");
 c.page=3;for(auto&q:fixture.quotas){q.state="unavailable";q.sampled=fixture.now-3600;}renderer.render(fixture,c,6000);save("08-stale-quota-fixture");
 c.page=1;fixture.codex.state="unavailable";fixture.codex.sampled=fixture.now-90;c.paused=true;renderer.render(fixture,c,0);save("09-paused-task-fixture");
 // Alibaba exhaustion is a displayed state, never a hidden page or a fake zero.
 fixture=model;c.paused=false;int ap=quotaPageOf(fixture,"aliyun-team");
 if(ap>=0){c.page=ap;for(auto&q:fixture.quotas)if(q.id=="aliyun-team"){q.availability=0;for(auto&w:q.windows)w.remaining=0;}
  renderer.render(fixture,c,6000);save("13-exhausted-aliyun-fixture");
  fixture=model;for(auto&q:fixture.quotas)if(q.id=="aliyun-team"){q.hasSnapshot=true;q.snapshotFresh=false;}
  renderer.render(fixture,c,6000);save("14-snapshot-stale-fixture");
  fixture=model;for(auto&q:fixture.quotas)if(q.id=="aliyun-team"){q.state="unavailable";q.sampled=0;q.validUntil=0;q.windows.clear();q.balances.clear();q.availability=-1;q.hasSnapshot=false;}
  c.page=ap;renderer.render(fixture,c,6000);save("16-aliyun-unknown-fixture");}
 // Wholly absent quota configuration hides every quota page; configured unknown rows never do.
 fixture=model;fixture.quotas.clear();board::Carousel hidden;hidden.page=2;renderer.render(fixture,hidden,6000);save("15-no-quota-tasks-fixture");
 {board::Carousel step;step.page=2;step.tick(fixture,15000);if(step.page!=0)std::exit(5);}
 board::Menu menu;menu.open=true;c.paused=true;model.ip="192.0.2.1";model.ssid="Example Wi-Fi";model.version="0.4.0";model.followMac=true;model.utcOffset=39600;
 renderer.render(model,c,4000,&menu);save("10-menu");menu.selected=3;renderer.render(model,c,4000,&menu);save("11-menu-timezone");menu.info=true;renderer.render(model,c,4000,&menu);save("12-device-info");
 *running=false;std::exit(0);
}
int main(int argc,char**argv){if(argc!=3)return 1;inputPath=argv[1];outputPath=argv[2];std::filesystem::create_directories(outputPath);return lgfx::Panel_sdl::main(run);}
