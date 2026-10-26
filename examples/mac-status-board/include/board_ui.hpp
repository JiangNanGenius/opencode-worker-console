#pragma once
#include "board_model.hpp"
#include <LovyanGFX.hpp>
#include <cstdio>
#include <ctime>
#include <array>
namespace board {
constexpr uint16_t rgb(int r,int g,int b){return ((r>>3)<<11)|((g>>2)<<5)|(b>>3);}
constexpr uint16_t BG=rgb(23,27,30),PANEL=rgb(34,40,44),FG=rgb(242,240,233),MUTED=rgb(174,184,188),GREEN=rgb(115,213,172),AMBER=rgb(243,190,105),RED=rgb(236,138,131),LINE=rgb(56,66,71);
inline std::string number(double x,int decimals=0){char b[48];snprintf(b,sizeof(b),"%.*f",decimals,x);return b;}
inline std::string durationText(double seconds){if(seconds<0)return "--";if(seconds>=86400)return number(seconds/86400,1)+"天";if(seconds>=3600)return number(seconds/3600,1)+"小时";if(seconds>=60)return number(seconds/60)+"分钟";return number(seconds)+"秒";}
inline std::string windowReset(double at,double now){if(at<=0)return "重置未知";if(at<=now)return "待刷新";double left=at-now;return durationText(left)+"后";}
inline std::string windowTime(const Window&w,double now){if(!w.expiry)return windowReset(w.reset,now);if(w.reset<=now)return "已到期";return durationText(w.reset-now)+"到期";}
inline std::vector<std::string> glyphs(const std::string&s){std::vector<std::string>v;for(size_t i=0;i<s.size();){uint8_t c=s[i];size_t n=c<0x80?1:c<0xe0?2:c<0xf0?3:4;if(i+n>s.size())break;v.push_back(s.substr(i,n));i+=n;}return v;}
class Renderer {
 lgfx::LGFX_Sprite stripe;lgfx::LGFX_Device&lcd;int origin=0,bandHeight=40;uint32_t hashes[12]={};bool drawn[12]={};int progressUnits=-1;
 Model frameModel;Carousel frameCarousel;Menu frameMenu;uint32_t frameMs=0;int nextBand=0;bool active=false,withMenu=false;
 // Fixed entry counts, bounded input strings and FIFO replacement prevent cache growth.
 struct WidthEntry{std::string key;const lgfx::IFont*font=nullptr;int value=0;};
 struct FitEntry{std::string key,value;int max=-1;};
 struct WrapEntry{std::string key,a,b;int max=-1;};
 std::array<WidthEntry,48> widths;std::array<FitEntry,16> fits;std::array<WrapEntry,4> wraps;unsigned widthSlot=0,fitSlot=0,wrapSlot=0;
 bool intersects(int y,int height)const{return y<origin+bandHeight&&y+height>origin;}
 int fontHeight(const lgfx::IFont*f)const{lgfx::FontMetrics metric;f->getDefaultMetric(&metric);return metric.height;}
 void rect(int x,int y,int w,int h,uint16_t c){if(intersects(y,h))stripe.fillRect(x,y-origin,w,h,c);}
 void line(int y){rect(16,y,288,1,LINE);}
 void text(const std::string&s,int x,int y,uint16_t c=FG,const lgfx::IFont*f=&fonts::efontCN_16){if(!intersects(y,fontHeight(f)))return;stripe.setTextSize(1);stripe.setFont(f);stripe.setTextColor(c,BG);stripe.drawString(s.c_str(),x,y-origin);}
 int measure(const std::string&s,const lgfx::IFont*f){stripe.setFont(f);stripe.setTextSize(1);return stripe.textWidth(s.c_str());}
 int width(const std::string&s,const lgfx::IFont*f=&fonts::efontCN_16){
  for(const auto&e:widths)if(e.font==f&&e.key==s)return e.value;
  int value=measure(s,f);if(s.size()<=288){auto&e=widths[widthSlot++%widths.size()];e.key=s;e.font=f;e.value=value;}return value;
 }
 void right(const std::string&s,int x,int y,uint16_t c=FG,const lgfx::IFont*f=&fonts::efontCN_16){if(!intersects(y,fontHeight(f)))return;text(s,x-width(s,f),y,c,f);}
 static size_t glyphEnd(const std::string&s,size_t i){uint8_t c=s[i];return std::min(s.size(),i+(c<0x80?1:c<0xe0?2:c<0xf0?3:4));}
 std::string fit(const std::string&s,int max){
  for(const auto&e:fits)if(e.max==max&&e.key==s)return e.value;
  std::string out=s;
  if(width(s)>max){out.clear();int used=0,ellipsis=width("...");for(size_t i=0;i<s.size();){size_t end=glyphEnd(s,i);std::string g=s.substr(i,end-i);int advance=measure(g,&fonts::efontCN_16);if(used+advance+ellipsis>max)break;used+=advance;out+=g;i=end;}out+="...";}
  if(s.size()<=288){auto&e=fits[fitSlot++%fits.size()];e.key=s;e.max=max;e.value=out;}return out;
 }
 void twoLines(const std::string&s,int x,int y,int max){
  if(!intersects(y,24+fontHeight(&fonts::efontCN_16)))return;
  for(const auto&e:wraps)if(e.max==max&&e.key==s){text(e.a,x,y);if(!e.b.empty())text(e.b,x,y+24);return;}
  std::string a,b;int used=0;size_t i=0;
  for(;i<s.size();){size_t end=glyphEnd(s,i);std::string g=s.substr(i,end-i);int advance=measure(g,&fonts::efontCN_16);if(used+advance>max)break;used+=advance;a+=g;i=end;}
  b=s.substr(i);if(!b.empty()){auto space=a.find_last_of(' ');if(space!=std::string::npos&&space>a.size()/2){b=a.substr(space+1)+b;a.resize(space);}b=fit(b,max);}
  if(s.size()<=288){auto&e=wraps[wrapSlot++%wraps.size()];e.key=s;e.max=max;e.a=a;e.b=b;}
  text(a,x,y);if(!b.empty())text(b,x,y+24);
 }
 void bar(int x,int y,int w,float percent,uint16_t c){rect(x,y,w,4,LINE);if(std::isfinite(percent))rect(x,y,int(w*std::min(100.f,std::max(0.f,percent))/100.f),4,c);}
 void top(const Model&m){
  if(m.now>1700000000){time_t t=time_t(m.now)+(m.followMac?m.utcOffset:28800);tm out;gmtime_r(&t,&out);char hh[8],sec[4],date[28];strftime(hh,sizeof(hh),"%H:%M",&out);strftime(sec,sizeof(sec),"%S",&out);snprintf(date,sizeof(date),"%02d/%02d %s",out.tm_mon+1,out.tm_mday,(const char*[]){"周日","周一","周二","周三","周四","周五","周六"}[out.tm_wday]);
   text(hh,16,7,FG,&fonts::Font7);text(sec,211,28,MUTED,&fonts::Font4);text(date,16,65,MUTED);
  } else {text("--:--",16,7,FG,&fonts::Font7);text("等待校时",16,65,AMBER);}
  int active=m.codex.active+m.opencode.active;
  bool fresh=sourceFresh(m.codex,m.now)&&sourceFresh(m.opencode,m.now);
  right(fresh?"任务 "+std::to_string(active):"任务 --",304,65,MUTED);
  stripe.fillCircle(296,35-origin,4,m.wifi?GREEN:AMBER);line(87);
 }
 void system(const Model&m){
  text("系统概览",16,102);right(fit(m.host,145),304,102,MUTED);
  if(!systemFresh(m)){text("Mac 数据已离线",16,169,AMBER);text("等待电脑恢复连接",16,199,MUTED);text("最后数据 "+(m.systemSampled?durationText(m.now-m.systemSampled)+"前":"尚未收到"),16,241,MUTED);return;}
  text("CPU",16,139,MUTED);text("内存",168,139,MUTED);
  text(number(m.cpu,1),16,166,FG,&fonts::Font4);text("%",94,172,MUTED);text(number(m.mem,1),168,166,FG,&fonts::Font4);text("%",246,172,MUTED);
  bar(16,202,136,m.cpu,GREEN);bar(168,202,136,m.mem,GREEN);
  right(number(m.memUsed,1)+" / "+number(m.memTotal,0)+" GB",304,213,MUTED);
  text("磁盘可用 / 总量",16,235,MUTED);
  for(int i=0;i<3;i++){int y=258+i*28;if(i<int(m.disks.size())){auto&d=m.disks[i];text(fit(d.name,125),16,y,MUTED);right(number(d.free,0)+" / "+number(d.total,0)+" GiB",304,y);bar(16,y+20,288,d.used,d.used>=90?AMBER:GREEN);}else{text("磁盘未挂载",16,y,MUTED);}}
  text("CPU 温度",16,347,MUTED);text("GPU 温度",168,347,MUTED);
  text(std::isfinite(m.cpuTemp)?number(m.cpuTemp,1)+" C":"--",16,370,FG,&fonts::Font4);text(std::isfinite(m.gpuTemp)?number(m.gpuTemp,1)+" C":"--",168,370,FG,&fonts::Font4);
  text("风扇",16,407,MUTED);right(m.fan>=0?std::to_string(m.fan)+" RPM":"不可用",304,407);text("已运行",16,429,MUTED);right(durationText(m.uptime*3600),304,429,MUTED);
 }
 void tasks(const Model&m,const Source&s,const char*name,int offset){
  text(name,16,100,FG,&fonts::Font4);bool fresh=sourceFresh(s,m.now);
  int pages=std::max(1,int((s.items.size()+1)/2));offset=pages?(offset%(pages*2)):0;
  right(std::to_string(offset/2+1)+" / "+std::to_string(pages),304,107,MUTED);
  text(!fresh?"来源失联 · 状态已过期":std::to_string(s.total)+" 项当前任务",16,137,fresh?MUTED:AMBER);
  if(s.items.empty()){text(fresh?"当前没有任务":"等待任务来源",16,212,MUTED);return;}
  for(int i=0;i<2;i++){int index=offset+i;if(index>=int(s.items.size()))break;auto&t=s.items[index];bool showModel=std::string(name)=="OpenCode";int cardHeight=showModel?143:132;int y=176+i*cardHeight;if(y>=origin+bandHeight||y+cardHeight<=origin)continue;
   const char*label=t.status=="attention"?"待处理":t.status=="running"?"运行中":t.status=="completed"?"已完成":"状态待确认";
   uint16_t color=!fresh?MUTED:t.status=="running"?GREEN:t.status=="completed"?MUTED:AMBER;
   text(label,16,y,color);double elapsed=(t.ended?t.ended:fresh?m.now:t.updated)-t.started;right(t.started?durationText(elapsed):"待开始",304,y,MUTED);
   twoLines(t.title,16,y+29,288);text(fit(t.project,288),16,y+84,MUTED);if(showModel)text(fit(t.model.empty()||t.model=="模型未知"?std::string("模型未知"):"模型 "+t.model,288),16,y+105,GREEN);if(i==0)line(y+(showModel?130:117));
  }
 }
 void supplier(const Model&m,int index,int y){
  if(y>=origin+bandHeight||y+136<=origin)return;
  if(index>=int(m.quotas.size()))return;  // Odd last slot: no provider is configured there.
  const Quota&q=m.quotas[index];bool fresh=quotaFresh(q,m.now);
  text(fit(q.name,170),16,y);std::string age=q.sampled?durationText(std::max(0.,m.now-q.sampled))+"前":"";
  if(!q.sampled)right("未接入",304,y,AMBER);
  else if(q.monthlyExhausted)right("月度耗尽",304,y,AMBER);
  else if(q.availability==0)right("已耗尽",304,y,AMBER);
  else if(!fresh)right(q.hasSnapshot?"快照过期":"旧 · "+age,304,y,AMBER);
  else right(q.hasSnapshot?"快照 · "+age:age,304,y,MUTED);
  if(!q.sampled){text("暂时无法读取",16,y+38,AMBER);return;}
  int ci=creditsWindow(q);
  if(ci>=0){const Window&w=q.windows[ci];uint16_t c=!fresh?MUTED:w.remaining<=10?AMBER:GREEN;
   text(number(w.amount,1),16,y+42,FG,&fonts::Font4);right(w.unit,304,y+48,MUTED);
   text("总额 "+number(w.limit,0),16,y+72,MUTED);right(number(w.remaining,1)+"%",177,y+72,c);right(windowTime(w,m.now),304,y+72,MUTED);
   int best=-1;for(int i=0;i<int(q.windows.size());i++){if(i==ci)continue;if(best<0||q.windows[i].remaining<q.windows[best].remaining)best=i;}
   if(best>=0){const Window&x=q.windows[best];uint16_t cc=!fresh?MUTED:x.remaining<=10?AMBER:GREEN;
    text(x.label,16,y+104,MUTED);right(number(x.remaining,1)+"%",177,y+104,cc);right(windowTime(x,m.now),304,y+104,MUTED);}
   return;}
  if(!q.balances.empty()){auto&b=q.balances[0];text(number(b.remaining,2),16,y+42,FG,&fonts::Font4);right(b.currency,304,y+48,MUTED);text("账户余额",16,y+79,MUTED);}
  for(size_t i=0;i<q.windows.size()&&i<4;i++){auto&w=q.windows[i];int yy=y+31+int(i)*26;uint16_t c=!fresh?MUTED:w.remaining<=10?AMBER:GREEN;
   text(w.label,16,yy,MUTED);right(number(w.remaining,1)+"%",177,yy,c);right(windowTime(w,m.now),304,yy,MUTED);
  }
  if(q.windows.empty()&&q.balances.empty())text("未提供额度窗口",16,y+40,MUTED);

 }
 void quotaPage(const Model&m,int page){int first=(page-3)*2,tp=std::max(1,Carousel::quotaPages(m));
  text("套餐额度",16,102);right(std::to_string(page-2)+" / "+std::to_string(tp),304,102,MUTED);text("剩余",133,127,MUTED);right("时间",304,127,MUTED);
  supplier(m,first,157);line(306);supplier(m,first+1,319);}
 static int progress(const Carousel&c,uint32_t ms){uint32_t elapsed=uint32_t(ms-c.began),duration=Carousel::duration(c.page);return c.paused||elapsed>=duration?1152:int(elapsed*1152/duration);}
 static uint16_t edgeColor(int coverage){
  // Blend in the panel's native RGB565 channels. Coverage is 0..4 quarter pixels.
  return uint16_t((((LINE>>11)*(4-coverage)+(GREEN>>11)*coverage+2)/4)<<11)|
         uint16_t(((((LINE>>5)&63)*(4-coverage)+((GREEN>>5)&63)*coverage+2)/4)<<5)|
         uint16_t(((LINE&31)*(4-coverage)+(GREEN&31)*coverage+2)/4);
 }
 void progressStrip(int units){int pixels=units/4;rect(16,478,288,2,LINE);rect(16,478,pixels,2,GREEN);if(units%4&&pixels<288)rect(16+pixels,478,1,2,edgeColor(units%4));}
 void footer(const Model&m,const Carousel&c,uint32_t ms){line(448);
  std::string label=c.page==0?"系统":c.page==1?"Codex":c.page==2?"OpenCode":"额度 "+std::to_string(c.page-2)+"/"+std::to_string(std::max(1,Carousel::quotaPages(m)));
  text(label,16,458,MUTED);
  bool fresh=systemFresh(m);if(c.page==1)fresh=sourceFresh(m.codex,m.now);if(c.page==2)fresh=sourceFresh(m.opencode,m.now);
  if(c.page>=3){int first=(c.page-3)*2;bool second=first+1<int(m.quotas.size());
   fresh=int(m.quotas.size())>first&&quotaFresh(m.quotas[first],m.now)&&(!second||quotaFresh(m.quotas[first+1],m.now));}
  right(c.paused?"已暂停":fresh?"实时同步":"数据过期",304,458,c.paused||!fresh?AMBER:GREEN);
  progressStrip(progress(c,ms));
 }
 void menuPage(const Model&m,const Menu&menu){
  text(menu.info?"设备信息":"快捷设置",16,102);
  if(menu.info){
   text("设备 IP",16,155,MUTED);text(m.wifi&&!m.ip.empty()?m.ip:"尚未联网",16,182,FG,&fonts::Font4);
   line(227);text("Wi-Fi",16,250,MUTED);right(fit(m.ssid,220),304,250);
   text("连接状态",16,288,MUTED);right(m.wifi?"已连接":"未连接",304,288,m.wifi?GREEN:AMBER);
   text("固件版本",16,326,MUTED);right(m.version,304,326);
   text("时区偏移",16,364,MUTED);int offset=m.followMac?m.utcOffset:28800;right("UTC"+std::string(offset>=0?"+":"-")+number(std::abs(offset)/3600.,2),304,364);
   text("长按返回设置",16,412,MUTED);return;
  }
  const char*labels[]={"设备信息","屏幕亮度","自动轮播","时间时区","返回信息牌"};
  for(int i=0;i<5;i++){int y=151+i*51;if(i==menu.selected)rect(12,y-8,296,43,PANEL);text(labels[i],24,y,i==menu.selected?GREEN:FG);
   if(i==0)right("IP / Wi-Fi",296,y,MUTED);
   if(i==1)right(std::to_string(menu.brightness)+"%",296,y,MUTED);
   if(i==2)right(menu.savedPaused?"已暂停":"开启",296,y,MUTED);
   if(i==3)right(m.followMac?"跟随 Mac":"北京时间",296,y,MUTED);
  }
  text("60 秒无操作自动返回",16,417,MUTED);
 }
 void menuFooter(const Menu&menu){line(448);text(menu.info?"短按或长按返回":"短按选择 · 长按确认",16,458,MUTED);}
public:
 explicit Renderer(lgfx::LGFX_Device&target,int height=40):stripe(&target),lcd(target),bandHeight(height){}
 bool begin(){if(bandHeight<40||480%bandHeight)return false;stripe.setColorDepth(16);return stripe.createSprite(320,bandHeight)!=nullptr;}
 void invalidate(){for(bool&x:drawn)x=false;}
 // Parsed models have fixed provider/window/task/string caps. Copy once per frame so
 // network readback and button navigation cannot mutate a stripe halfway through it.
 bool startFrame(const Model&m,const Carousel&c,uint32_t ms,const Menu*menu=nullptr){
  if(active)return false;frameModel=m;frameCarousel=c;frameMs=ms;withMenu=menu&&menu->open;if(withMenu)frameMenu=*menu;nextBand=0;active=true;return true;
 }
 bool frameActive()const{return active;}
 bool renderStep(){
  if(!active)return false;
  const Model&m=frameModel;const Carousel&c=frameCarousel;origin=nextBand*bandHeight;stripe.fillScreen(BG);if(origin<88)top(m);
  int clipTop=std::max(0,88-origin),clipBottom=std::min(bandHeight,448-origin);
  if(clipBottom>clipTop){stripe.setClipRect(0,clipTop,320,clipBottom-clipTop);if(withMenu)menuPage(m,frameMenu);else if(c.page==0)system(m);else if(c.page==1)tasks(m,m.codex,"Codex",c.codexOffset);else if(c.page==2)tasks(m,m.opencode,"OpenCode",c.openOffset);else quotaPage(m,c.page);stripe.clearClipRect();}
  if(origin+bandHeight>448){if(withMenu)menuFooter(frameMenu);else footer(m,c,frameMs);}
  // Sprite storage is 32-bit aligned; hash one native word per iteration rather
  // than two multiply-dependent byte operations per pixel.
  auto*words=static_cast<const uint32_t*>(stripe.getBuffer());uint32_t hash=2166136261u;
  for(size_t i=0;i<size_t(320*bandHeight/2);i++)hash=(hash^words[i])*16777619u;
  if(!drawn[nextBand]||hashes[nextBand]!=hash){stripe.pushSprite(0,origin);hashes[nextBand]=hash;drawn[nextBand]=true;}
  if(++nextBand<480/bandHeight)return false;
  if(!withMenu)progressUnits=progress(c,frameMs);else progressUnits=-1;active=false;return true;
 }
 void render(const Model&m,const Carousel&c,uint32_t ms,const Menu*menu=nullptr){
  if(!startFrame(m,c,ms,menu))return;while(!renderStep()){}
 }
 // The latest compatible progress timestamp is used when the footer stripe is
 // eventually painted. Main can keep ticking progress between individual stripes.
 bool renderProgress(const Carousel&c,uint32_t ms){
  if(active){if(withMenu||c.page!=frameCarousel.page||c.began!=frameCarousel.began||c.paused!=frameCarousel.paused)return false;frameMs=ms;}
  int next=progress(c,ms);if(progressUnits<0||next==progressUnits)return false;
  int first=std::min(next,progressUnits)/4,last=std::min(288,std::max(next,progressUnits)/4+1),solid=next/4;
  lcd.fillRect(16+first,478,last-first,2,LINE);
  if(solid>first)lcd.fillRect(16+first,478,std::min(last,solid)-first,2,GREEN);
  if(next%4&&solid>=first&&solid<last)lcd.fillRect(16+solid,478,1,2,edgeColor(next%4));
  progressUnits=next;drawn[478/bandHeight]=false;return true;
 }
};
}
