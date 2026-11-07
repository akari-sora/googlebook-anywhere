#define main shim_main
#include "shim.cpp"
#undef main
#include <cassert>
#include <cstdio>
#include <thread>
#include <atomic>
struct AIBinder { std::atomic<bool> alive{true}; std::atomic<int> refs{1}; std::atomic<int> calls{0}; void* cookie=nullptr; }; struct AIBinder_Class {}; struct AStatus { int exception; }; struct AParcel { AIBinder* binder=nullptr; int status=0; bool value=false; bool malformed=false; };
struct AIBinder_DeathRecipient { void(*died)(void*); void(*unlinked)(void*); };
static std::mutex mocklock; static bool reenter=false; static std::atomic<bool> pause_link{false}, link_entered{false}, release_link{false};
static AIBinder_Class cls; static AIBinder_DeathRecipient rec{died,unlinked};
AIBinder_DeathRecipient* AIBinder_DeathRecipient_new(void(*f)(void*)){rec.died=f;return &rec;}
void AIBinder_DeathRecipient_setOnUnlinked(AIBinder_DeathRecipient* r,void(*f)(void*)){r->unlinked=f;}
int AIBinder_linkToDeath(AIBinder* b,AIBinder_DeathRecipient* r,void* cookie){if(pause_link){link_entered=true;while(!release_link)std::this_thread::yield();}std::lock_guard<std::mutex> g(mocklock);if(!b->alive){r->unlinked(cookie);return -3;}b->cookie=cookie;return 0;}
int AIBinder_unlinkToDeath(AIBinder* b,AIBinder_DeathRecipient* r,void* cookie){{std::lock_guard<std::mutex> g(mocklock);if(b->cookie!=cookie)return -4;b->cookie=nullptr;}r->unlinked(cookie);return 0;}
static void die(AIBinder& b){void* c;{std::lock_guard<std::mutex> g(mocklock);b.alive=false;c=b.cookie;b.cookie=nullptr;}if(c){rec.died(c);rec.unlinked(c);}}
AIBinder_Class* AIBinder_Class_define(const char*,void*(*)(void*),void(*)(void*),binder_status_t(*)(AIBinder*,transaction_code_t,const AParcel*,AParcel*)) {return &cls;}
AIBinder* AIBinder_new(AIBinder_Class*,void*) {return new AIBinder;}
void AIBinder_incStrong(AIBinder* b){++b->refs;} void AIBinder_decStrong(AIBinder* b){--b->refs;} bool AIBinder_isAlive(const AIBinder* b){return b->alive;}
bool AIBinder_associateClass(AIBinder*,const AIBinder_Class*){return true;} uint32_t AIBinder_getCallingUid(){return 1000;}
int AIBinder_prepareTransaction(AIBinder*,AParcel** p){*p=new AParcel;return STATUS_OK;}
int AIBinder_transact(AIBinder* b,transaction_code_t,AParcel** req,AParcel**,uint32_t){delete *req;*req=nullptr;++b->calls;if(reenter){reenter=false;AParcel in,out;in.binder=b;assert(transact(nullptr,4,&in,&out)==0 && out.value);}return b->alive?STATUS_OK:-3;}
AStatus* AStatus_fromExceptionCode(int e){return new AStatus{e};} void AStatus_delete(AStatus* s){delete s;}
int AParcel_writeStatusHeader(AParcel* p,const AStatus* s){p->status=s->exception;return 0;} int AParcel_writeBool(AParcel* p,bool b){p->value=b;return 0;}
int AParcel_readStrongBinder(const AParcel* p,AIBinder** b){if(p->malformed)return -7;*b=p->binder;if(*b)AIBinder_incStrong(*b);return 0;}
int AParcel_writeInt32(AParcel*,int32_t){return 0;} int AParcel_writeFloat(AParcel*,float){return 0;} void AParcel_delete(AParcel* p){delete p;}
int __android_log_print(int,const char*,const char*,...){return 0;}
static bool call(int code,AIBinder* b){AParcel in,out;in.binder=b;assert(transact(nullptr,code,&in,&out)==0);assert(out.status==EX_NONE);return out.value;}
int main(){callback_class=&cls;death_recipient=&rec;AIBinder b;assert(call(3,&b));assert(call(3,&b));assert(b.refs==2 && b.calls==2 && callbacks.size()==1);assert(call(4,&b));assert(!call(4,&b));assert(b.refs==1);assert(!call(3,nullptr));assert(!call(4,nullptr)); AParcel bad,out;bad.malformed=true;assert(transact(nullptr,3,&bad,&out)==-7);for(int n=1;n<=14;++n)if(n!=3&&n!=4){AParcel in,res;assert(transact(nullptr,n,&in,&res)==0);assert(res.status==(n==11?EX_NONE:EX_SECURITY));if(n==11)assert(!res.value);}assert(transact(nullptr,15,&bad,&out)==STATUS_UNKNOWN_TRANSACTION);
 AIBinder items[65];for(int n=0;n<64;++n)assert(call(3,&items[n]));assert(!call(3,&items[64]));die(items[0]);assert(call(3,&items[64]));assert(items[0].refs==1 && callbacks.size()==64);for(int n=1;n<65;++n)assert(call(4,&items[n]));assert(callbacks.empty());
 AIBinder pending;pause_link=true;link_entered=false;release_link=false;bool first=true;
 std::thread t1([&]{first=call(3,&pending);});while(!link_entered)std::this_thread::yield();
 assert(!call(3,&pending));pending.alive=false;release_link=true;t1.join();assert(!first && callbacks.empty() && pending.refs==1);pause_link=false;
 AIBinder pending_cancel;pause_link=true;link_entered=false;release_link=false;bool cancelled=true;
 std::thread t2([&]{cancelled=call(3,&pending_cancel);});while(!link_entered)std::this_thread::yield();
 assert(call(4,&pending_cancel));release_link=true;t2.join();assert(!cancelled && callbacks.empty() && pending_cancel.refs==1);pause_link=false;
 AIBinder nested;reenter=true;assert(call(3,&nested));assert(callbacks.empty() && nested.refs==1);
 for(int n=0;n<200;++n){AIBinder race;assert(call(3,&race));std::thread t([&]{die(race);});call(4,&race);t.join();assert(callbacks.empty() && race.refs==1);}
 puts("PASS duplicate/null/malformed/capacity/dead-reclaim/unbind/deny-OTA");}
