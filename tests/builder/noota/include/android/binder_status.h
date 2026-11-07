#pragma once
#include "binder_ibinder.h"
constexpr int EX_NONE=0,EX_SECURITY=-1; struct AStatus; AStatus* AStatus_fromExceptionCode(binder_exception_t); void AStatus_delete(AStatus*);
