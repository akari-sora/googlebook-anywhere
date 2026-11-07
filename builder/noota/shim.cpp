#include <android/binder_ibinder.h>
#include <android/binder_parcel.h>
#include <dlfcn.h>
#include <android/log.h>
#include <android/binder_status.h>
#include <algorithm>
#include <cstdio>
#include <mutex>
#include <memory>
#include <vector>

static std::mutex lock;
struct Entry {
    explicit Entry(AIBinder* b): binder(b) { AIBinder_incStrong(binder); }
    ~Entry() { AIBinder_decStrong(binder); }
    AIBinder* binder;
    void* cookie=nullptr;
    bool linked=false;
    bool cancelled=false;
};
static std::vector<std::shared_ptr<Entry>> callbacks;
static AIBinder_DeathRecipient* death_recipient;
using Cookie = std::shared_ptr<Entry>;
static void died(void* cookie) {
    auto entry=*static_cast<Cookie*>(cookie);
    std::lock_guard<std::mutex> hold(lock);
    entry->cancelled=true;
    callbacks.erase(std::remove(callbacks.begin(),callbacks.end(),entry),callbacks.end());
}
static void unlinked(void* cookie) { delete static_cast<Cookie*>(cookie); }
static AIBinder_Class* callback_class;
static void* create(void* p) { return p; }
static void destroy(void*) {}
static binder_status_t callback_unused(AIBinder*, transaction_code_t, const AParcel*, AParcel*) { return STATUS_UNKNOWN_TRANSACTION; }
static binder_status_t header(AParcel* out, binder_exception_t exception) {
    AStatus* st=AStatus_fromExceptionCode(exception);
    if (!st) return STATUS_NO_MEMORY;
    binder_status_t rc=AParcel_writeStatusHeader(out,st);
    AStatus_delete(st);
    return rc;
}
static binder_status_t transact(AIBinder*, transaction_code_t code, const AParcel* in, AParcel* out) {
    if (code < 1 || code > 14) return STATUS_UNKNOWN_TRANSACTION;
    __android_log_print(ANDROID_LOG_ERROR,"GBOS_UPDATE","GBOS_UPDATE_TX code=%u uid=%u",code,AIBinder_getCallingUid());
    if (code==11) {
        binder_status_t rc=header(out,EX_NONE);
        return rc==STATUS_OK?AParcel_writeBool(out,false):rc;
    }
    if (code!=3 && code!=4) return header(out,EX_SECURITY);
    AIBinder* cb=nullptr;
    binder_status_t rc=AParcel_readStrongBinder(in,&cb);
    if(rc!=STATUS_OK) return rc;
    bool ok=false;
    if(cb && code==4) {
        std::shared_ptr<Entry> entry;
        bool unlink=false;
        {
            std::lock_guard<std::mutex> hold(lock);
            auto it=std::find_if(callbacks.begin(),callbacks.end(),[&](const auto& e){return e->binder==cb;});
            if(it!=callbacks.end()) {
                entry=*it; entry->cancelled=true; unlink=entry->linked;
                callbacks.erase(it); ok=true;
            }
        }
        if(unlink) AIBinder_unlinkToDeath(cb,death_recipient,entry->cookie);
    } else if(cb) {
        std::shared_ptr<Entry> entry;
        bool fresh=false;
        {
            std::lock_guard<std::mutex> hold(lock);
            auto it=std::find_if(callbacks.begin(),callbacks.end(),[&](const auto& e){return e->binder==cb;});
            if(it!=callbacks.end() && (*it)->linked) entry=*it;
            else if(it!=callbacks.end()) { /* Registration is still pending; do not report success. */ }
            else if(callbacks.size()<64) { entry=std::make_shared<Entry>(cb); callbacks.push_back(entry); fresh=true; }
        }
        if(entry && fresh) {
            // Ownership of this cookie transfers to onUnlinked even when link fails.
            auto cookie=new Cookie(entry);
            entry->cookie=cookie;
            rc=AIBinder_linkToDeath(cb,death_recipient,cookie);
            bool cancel;
            {
                std::lock_guard<std::mutex> hold(lock);
                cancel=entry->cancelled;
                if(rc==STATUS_OK && !cancel) entry->linked=true;
                else { entry->cancelled=true; callbacks.erase(std::remove(callbacks.begin(),callbacks.end(),entry),callbacks.end()); }
            }
            if(rc==STATUS_OK && cancel) AIBinder_unlinkToDeath(cb,death_recipient,cookie);
            if(rc!=STATUS_OK || cancel) entry.reset();
        }
        if(entry && AIBinder_associateClass(cb,callback_class)) {
            AParcel* req=nullptr; AParcel* resp=nullptr;
            rc=AIBinder_prepareTransaction(cb,&req);
            if(rc==STATUS_OK) rc=AParcel_writeInt32(req,0);
            if(rc==STATUS_OK) rc=AParcel_writeFloat(req,0.0f);
            if(rc==STATUS_OK) rc=AIBinder_transact(cb,1,&req,&resp,FLAG_ONEWAY);
            if(req) AParcel_delete(req);
            if(resp) AParcel_delete(resp);
            ok=(rc==STATUS_OK);
        }
        if(entry && !ok) {
            bool unlink=false;
            {
                std::lock_guard<std::mutex> hold(lock);
                auto it=std::find(callbacks.begin(),callbacks.end(),entry);
                if(it!=callbacks.end()) { entry->cancelled=true; unlink=entry->linked; callbacks.erase(it); }
            }
            if(unlink) AIBinder_unlinkToDeath(cb,death_recipient,entry->cookie);
        }
    }
    if(cb) AIBinder_decStrong(cb);
    rc=header(out,EX_NONE);
    return rc==STATUS_OK?AParcel_writeBool(out,ok):rc;
}
int main() {
    death_recipient=AIBinder_DeathRecipient_new(died);
    if(!death_recipient) return 1;
    AIBinder_DeathRecipient_setOnUnlinked(death_recipient,unlinked);
    callback_class=AIBinder_Class_define("android.os.IUpdateEngineCallback",create,destroy,callback_unused);
    AIBinder_Class* klass=AIBinder_Class_define("android.os.IUpdateEngine",create,destroy,transact);
    if(!callback_class||!klass) return 1;
    const char* name="android.os.UpdateEngineService";
    using Check = AIBinder* (*)(const char*);
    using Add = binder_status_t (*)(AIBinder*, const char*);
    using Threads = void (*)(uint32_t);
    using Join = void (*)();
    auto check=reinterpret_cast<Check>(dlsym(RTLD_DEFAULT,"AServiceManager_checkService"));
    auto add=reinterpret_cast<Add>(dlsym(RTLD_DEFAULT,"AServiceManager_addService"));
    auto threads=reinterpret_cast<Threads>(dlsym(RTLD_DEFAULT,"ABinderProcess_setThreadPoolMaxThreadCount"));
    auto join=reinterpret_cast<Join>(dlsym(RTLD_DEFAULT,"ABinderProcess_joinThreadPool"));
    if(!check||!add||!threads||!join) {__android_log_print(ANDROID_LOG_ERROR,"GBOS_UPDATE","platform binder APIs absent"); return 6;}
    AIBinder* existing=check(name);
    if(existing) { AIBinder_decStrong(existing); __android_log_print(ANDROID_LOG_ERROR,"GBOS_UPDATE","existing service; refusing"); return 2; }
    AIBinder* service=AIBinder_new(klass,nullptr);
    if(!service) return 3;
    binder_status_t rc=add(service,name);
    if(rc!=STATUS_OK) {__android_log_print(ANDROID_LOG_ERROR,"GBOS_UPDATE","registration failed %d",rc);return 4;}
    __android_log_print(ANDROID_LOG_ERROR,"GBOS_UPDATE","GBOS_UPDATE_READY");
    threads(4);
    join();
    return 5;
}
