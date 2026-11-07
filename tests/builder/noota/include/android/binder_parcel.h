#pragma once
#include "binder_ibinder.h"
struct AStatus; binder_status_t AParcel_writeStatusHeader(AParcel*,const AStatus*); binder_status_t AParcel_writeBool(AParcel*,bool); binder_status_t AParcel_readStrongBinder(const AParcel*,AIBinder**); binder_status_t AParcel_writeInt32(AParcel*,int32_t); binder_status_t AParcel_writeFloat(AParcel*,float); void AParcel_delete(AParcel*);
