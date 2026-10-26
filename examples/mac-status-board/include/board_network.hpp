#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include "board_protocol.hpp"
namespace board {
struct NetworkStatus {bool connected=false;uint32_t reconnects=0,received=0,lastReceived=0,scans=0,authFailures=0;char peer[16]={},method[16]={};};
// All discovery, connect and receive work stays on core 0. Only the bounded
// single-slot mailbox crosses into the render loop; this class never touches UI.
class Network {
 SemaphoreHandle_t mutex=nullptr;TaskHandle_t task=nullptr;std::string key,pending;NetworkStatus stats,cachedStatus;Preferences storage;
 IPAddress cached;uint32_t lastScan=0;bool scanned=false,mdnsReady=false;WiFiClient client;
 static void worker(void*p){static_cast<Network*>(p)->run();}
 static uint32_t address(IPAddress ip){return(uint32_t(ip[0])<<24)|(uint32_t(ip[1])<<16)|(uint32_t(ip[2])<<8)|ip[3];}
 static bool allowed(IPAddress ip){return protocol::lanAddress(address(ip));}
 void method(const char*name){xSemaphoreTake(mutex,portMAX_DELAY);strlcpy(stats.method,name,sizeof(stats.method));xSemaphoreGive(mutex);}
 std::string secret(){xSemaphoreTake(mutex,portMAX_DELAY);std::string s=key;xSemaphoreGive(mutex);return s;}
 static std::string nonce(){uint8_t bytes[16];esp_fill_random(bytes,sizeof(bytes));static const char hex[]="0123456789abcdef";std::string s(32,'0');for(int i=0;i<16;++i){s[i*2]=hex[bytes[i]>>4];s[i*2+1]=hex[bytes[i]&15];}return s;}
 bool send(JsonDocument&doc){std::string line;serializeJson(doc,line);line+='\n';return client.write(reinterpret_cast<const uint8_t*>(line.data()),line.size())==line.size();}
 bool receive(std::string&line,uint32_t timeout=3000){line.clear();uint32_t start=millis();while(uint32_t(millis()-start)<timeout&&WiFi.status()==WL_CONNECTED){while(client.available()){int c=client.read();if(c=='\n')return !line.empty();if(c<0)break;if(line.size()>=protocol::MaxFrame)return false;line+=char(c);}if(!client.connected())return false;vTaskDelay(pdMS_TO_TICKS(4));}return false;}
 bool connect(IPAddress ip,const char*via){if(!allowed(ip)||ip==WiFi.localIP())return false;std::string localKey=secret();if(localKey.size()<24)return false;
  client.stop();if(!client.connect(ip,protocol::Port,75))return false;client.setNoDelay(true);client.setTimeout(3);method(via);
  std::string boardNonce=nonce(),line;JsonDocument doc;doc["v"]=1;doc["type"]="hello";doc["nonce"]=boardNonce;if(!send(doc)||!receive(line))return failed();
  doc.clear();if(deserializeJson(doc,line)||doc["v"]!=1||doc["type"]!="challenge")return failed();std::string hostNonce=doc["nonce"].as<std::string>(),signature=doc["proof"].as<std::string>();
  if(!protocol::nonceValid(hostNonce)||!protocol::equal(signature,protocol::proof(localKey,"host",boardNonce,hostNonce)))return failed();
  doc.clear();doc["type"]="auth";doc["proof"]=protocol::proof(localKey,"board",boardNonce,hostNonce);if(!send(doc))return failed();
  // A verified host must also deliver a well-formed first snapshot. Neither an
  // unverified offer nor a TCP accept changes the remembered authenticated peer.
  if(!snapshot(line,doc,0))return failed();
  if(ip!=cached){cached=ip;storage.putString("peer",ip.toString());}
  xSemaphoreTake(mutex,portMAX_DELAY);stats.connected=true;strlcpy(stats.peer,ip.toString().c_str(),sizeof(stats.peer));xSemaphoreGive(mutex);
  uint32_t seq=doc["seq"]|0U;while(client.connected()&&WiFi.status()==WL_CONNECTED){if(!snapshot(line,doc,seq))break;seq=doc["seq"]|0U;}
  client.stop();xSemaphoreTake(mutex,portMAX_DELAY);stats.connected=false;++stats.reconnects;xSemaphoreGive(mutex);return true;
 }
 bool failed(){client.stop();xSemaphoreTake(mutex,portMAX_DELAY);++stats.authFailures;xSemaphoreGive(mutex);return false;}
 bool snapshot(std::string&line,JsonDocument&doc,uint32_t previous){if(!receive(line,4000))return false;doc.clear();if(deserializeJson(doc,line)||doc["v"]!=1||doc["type"]!="snapshot"||!doc["seq"].is<uint32_t>()||!doc["data"].is<JsonObject>())return false;
  uint32_t seq=doc["seq"].as<uint32_t>();if(seq<=previous||doc["data"]["schema_version"]!=2||(!doc["data"]["system"]["cpu"].is<float>()&&!doc["data"]["system"]["cpu"].is<int>()))return false;std::string value;serializeJson(doc["data"],value);if(value.size()>protocol::MaxFrame)return false;
  xSemaphoreTake(mutex,portMAX_DELAY);pending=std::move(value);++stats.received;stats.lastReceived=millis();xSemaphoreGive(mutex);
  JsonDocument ack;ack["type"]="ack";ack["seq"]=seq;return send(ack);
 }
 bool udp(){method("udp");WiFiUDP udp;if(!udp.begin(0))return false;std::string boardNonce=nonce(),localKey=secret(),line;JsonDocument doc;doc["v"]=1;doc["type"]="probe";doc["nonce"]=boardNonce;serializeJson(doc,line);
  IPAddress broadcast=WiFi.localIP();IPAddress mask=WiFi.subnetMask();for(int i=0;i<4;++i)broadcast[i]|=~mask[i];udp.beginPacket(broadcast,protocol::Port);udp.write(reinterpret_cast<const uint8_t*>(line.data()),line.size());udp.endPacket();
  uint32_t began=millis();while(uint32_t(millis()-began)<900){int size=udp.parsePacket();if(size>0){IPAddress ip=udp.remoteIP();if(size>512||!allowed(ip)){while(udp.available())udp.read();continue;}char buffer[513];int count=udp.read(buffer,512);buffer[count]=0;doc.clear();if(deserializeJson(doc,buffer,count)||doc["v"]!=1||doc["type"]!="offer"||doc["port"]!=protocol::Port)continue;
    std::string hostNonce=doc["nonce"].as<std::string>(),signature=doc["proof"].as<std::string>();if(protocol::nonceValid(hostNonce)&&protocol::equal(signature,protocol::proof(localKey,"offer",boardNonce,hostNonce,true))){udp.stop();return connect(ip,"udp");}}
   vTaskDelay(pdMS_TO_TICKS(10));}udp.stop();return false;
 }
 bool discover(){if(cached!=IPAddress()&&connect(cached,"cached"))return true;
  method("mdns");int count=MDNS.queryService("macboard","tcp");for(int i=0;i<count&&i<8;++i)if(MDNS.port(i)==protocol::Port&&connect(MDNS.IP(i),"mdns"))return true;
  if(udp())return true;
  uint32_t now=millis();if(scanned&&uint32_t(now-lastScan)<60000)return false;lastScan=now;scanned=true;method("scan");xSemaphoreTake(mutex,portMAX_DELAY);++stats.scans;xSemaphoreGive(mutex);
  IPAddress own=WiFi.localIP(),mask=WiFi.subnetMask(); // bounded /24 intersection, never scan an unrelated subnet
  for(int host=1;host<=254&&WiFi.status()==WL_CONNECTED;++host){IPAddress ip=own;ip[3]=host;bool same=true;for(int i=0;i<4;++i)if((ip[i]&mask[i])!=(own[i]&mask[i]))same=false;if(same&&ip!=own&&connect(ip,"scan"))return true;vTaskDelay(pdMS_TO_TICKS(1));}return false;
 }
 void run(){storage.begin("statusboard",false);cached.fromString(storage.getString("peer",""));uint32_t backoff=2000;
  for(;;){if(WiFi.status()!=WL_CONNECTED||secret().size()<24){vTaskDelay(pdMS_TO_TICKS(500));continue;}
   if(!mdnsReady){mdnsReady=MDNS.begin("mac-status-board");if(mdnsReady)MDNS.addService("http","tcp",80);}
   bool streamed=discover();uint32_t wait=streamed?2000:backoff;backoff=streamed?2000:std::min(uint32_t(60000),backoff*2);vTaskDelay(pdMS_TO_TICKS(wait));}
 }
public:
 void begin(const String&token){mutex=xSemaphoreCreateMutex();key=token.c_str();xTaskCreatePinnedToCore(worker,"board-network",8192,this,1,&task,0);}
 void setKey(const String&token){if(!mutex)return;xSemaphoreTake(mutex,portMAX_DELAY);key=token.c_str();xSemaphoreGive(mutex);}
 bool takeSnapshot(std::string&out){if(!mutex||xSemaphoreTake(mutex,0)!=pdTRUE)return false;bool ready=!pending.empty();if(ready){out=std::move(pending);pending.clear();}xSemaphoreGive(mutex);return ready;}
 NetworkStatus status(){if(mutex&&xSemaphoreTake(mutex,0)==pdTRUE){cachedStatus=stats;xSemaphoreGive(mutex);}return cachedStatus;}
};
}
