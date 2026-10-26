#pragma once
#include <cstdint>
#include <cstring>
#include <cmath>
#include <string>
#ifdef ARDUINO
#include <mbedtls/sha256.h>
#endif
namespace board { namespace protocol {
constexpr uint16_t Port=8642;
constexpr size_t MaxFrame=24576;
// Protocol/status timestamps are integer seconds: large compact floats lose low digits.
inline int64_t epochSeconds(double value){return std::isfinite(value)&&value>=0&&value<=4102444800.?int64_t(value):0;}
inline bool nonceValid(const std::string&v){if(v.size()!=32)return false;for(char c:v)if(!((c>='0'&&c<='9')||(c>='a'&&c<='f')))return false;return true;}
inline bool equal(const std::string&a,const std::string&b){if(a.size()!=b.size())return false;uint8_t d=0;for(size_t i=0;i<a.size();++i)d|=uint8_t(a[i]^b[i]);return d==0;}
inline void sha256(const uint8_t*data,size_t size,uint8_t*out){
#ifdef ARDUINO
 mbedtls_sha256_ret(data,size,out,0);
#else
 // Small native implementation keeps the cross-language protocol test dependency-free.
 static const uint32_t k[64]={0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
 uint32_t h[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
 auto r=[](uint32_t x,int n){return(x>>n)|(x<<(32-n));};size_t blocks=(size+9+63)/64;
 for(size_t block=0;block<blocks;++block){uint8_t bytes[64]={};for(size_t j=0;j<64;++j){size_t i=block*64+j;if(i<size)bytes[j]=data[i];else if(i==size)bytes[j]=0x80;}
  if(block==blocks-1){uint64_t bits=uint64_t(size)*8;for(int i=0;i<8;++i)bytes[63-i]=uint8_t(bits>>(i*8));}
  uint32_t w[64];for(int i=0;i<16;++i)w[i]=(uint32_t(bytes[4*i])<<24)|(uint32_t(bytes[4*i+1])<<16)|(uint32_t(bytes[4*i+2])<<8)|bytes[4*i+3];
  for(int i=16;i<64;++i){uint32_t a=w[i-15],b=w[i-2];w[i]=w[i-16]+(r(a,7)^r(a,18)^(a>>3))+w[i-7]+(r(b,17)^r(b,19)^(b>>10));}
  uint32_t a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],hh=h[7];for(int i=0;i<64;++i){uint32_t t=hh+(r(e,6)^r(e,11)^r(e,25))+((e&f)^(~e&g))+k[i]+w[i],u=(r(a,2)^r(a,13)^r(a,22))+((a&b)^(a&c)^(b&c));hh=g;g=f;f=e;e=d+t;d=c;c=b;b=a;a=t+u;}
  h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=hh;}
 for(int i=0;i<8;++i)for(int j=0;j<4;++j)out[i*4+j]=uint8_t(h[i]>>(24-j*8));
#endif
}
inline std::string hmac(const std::string&key,const std::string&message){uint8_t block[64]={},digest[32],inner[32];if(key.size()>64)sha256(reinterpret_cast<const uint8_t*>(key.data()),key.size(),block);else memcpy(block,key.data(),key.size());
 std::string input(64,'\0');for(int i=0;i<64;++i)input[i]=char(block[i]^0x36);input+=message;sha256(reinterpret_cast<const uint8_t*>(input.data()),input.size(),inner);
 input.resize(96);for(int i=0;i<64;++i)input[i]=char(block[i]^0x5c);memcpy(&input[64],inner,32);sha256(reinterpret_cast<const uint8_t*>(input.data()),input.size(),digest);
 static const char hex[]="0123456789abcdef";std::string output(64,'0');for(int i=0;i<32;++i){output[i*2]=hex[digest[i]>>4];output[i*2+1]=hex[digest[i]&15];}return output;}
inline std::string proof(const std::string&key,const char*role,const std::string&boardNonce,const std::string&serverNonce,bool offer=false){return hmac(key,std::string(role)+":v1:"+boardNonce+":"+serverNonce+(offer?":8642":""));}
inline bool lanAddress(uint32_t ip){uint8_t a=ip>>24,b=ip>>16;return a==10||a==127||(a==172&&b>=16&&b<=31)||(a==192&&b==168);}
}}
