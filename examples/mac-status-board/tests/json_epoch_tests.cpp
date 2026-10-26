#include <ArduinoJson.h>
#include "board_protocol.hpp"
#include <cassert>
#include <iostream>
#include <limits>

int main(){
 for(double epoch:{1795708800.,1795046400.,1793030400.}){
  // Regression: an exactly representable float32 epoch is stored compactly, then
  // six-place scientific formatting corrupts low seconds during re-serialization.
  JsonDocument original;original["epoch"]=epoch;std::string rounded;serializeJson(original,rounded);
  JsonDocument observed;assert(!deserializeJson(observed,rounded));assert(observed["epoch"].as<double>()!=epoch);
  // Host wire normalization: JSON integer -> network task repack -> main parse.
  std::string incoming="{\"epoch\":"+std::to_string(int64_t(epoch))+"}";
  JsonDocument network;assert(!deserializeJson(network,incoming));std::string mailbox;serializeJson(network,mailbox);
  JsonDocument main;assert(!deserializeJson(main,mailbox));assert(main["epoch"].as<int64_t>()==int64_t(epoch));
  // Status must explicitly assign an integer even when the model uses double.
  JsonDocument status;status["epoch"]=board::protocol::epochSeconds(main["epoch"].as<double>());std::string output;serializeJson(status,output);
  JsonDocument readback;assert(!deserializeJson(readback,output));assert(readback["epoch"].as<int64_t>()==int64_t(epoch));
  assert(output=="{\"epoch\":"+std::to_string(int64_t(epoch))+"}");
  std::cout<<int64_t(epoch)<<" bad="<<rounded<<" wire="<<mailbox<<" status="<<output<<"\n";
 }
 assert(board::protocol::epochSeconds(1795708800.875)==1795708800);
 assert(board::protocol::epochSeconds(-1)==0);
 assert(board::protocol::epochSeconds(std::numeric_limits<double>::infinity())==0);
 assert(board::protocol::epochSeconds(1e30)==0);
}
