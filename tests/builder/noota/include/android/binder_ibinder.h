#pragma once
#include <cstdint>
struct AIBinder; struct AIBinder_Class; struct AParcel;
using binder_status_t=int32_t; using transaction_code_t=uint32_t; using binder_exception_t=int32_t;
constexpr int STATUS_OK=0, STATUS_UNKNOWN_TRANSACTION=-1, STATUS_NO_MEMORY=-2, FLAG_ONEWAY=1;
AIBinder_Class* AIBinder_Class_define(const char*,void*(*)(void*),void(*)(void*),binder_status_t(*)(AIBinder*,transaction_code_t,const AParcel*,AParcel*));
AIBinder* AIBinder_new(AIBinder_Class*,void*); void AIBinder_incStrong(AIBinder*); void AIBinder_decStrong(AIBinder*); bool AIBinder_isAlive(const AIBinder*); bool AIBinder_associateClass(AIBinder*,const AIBinder_Class*); uint32_t AIBinder_getCallingUid();
binder_status_t AIBinder_prepareTransaction(AIBinder*,AParcel**); binder_status_t AIBinder_transact(AIBinder*,transaction_code_t,AParcel**,AParcel**,uint32_t);
struct AIBinder_DeathRecipient;
AIBinder_DeathRecipient* AIBinder_DeathRecipient_new(void(*)(void*));
void AIBinder_DeathRecipient_setOnUnlinked(AIBinder_DeathRecipient*,void(*)(void*));
binder_status_t AIBinder_linkToDeath(AIBinder*,AIBinder_DeathRecipient*,void*);
binder_status_t AIBinder_unlinkToDeath(AIBinder*,AIBinder_DeathRecipient*,void*);
