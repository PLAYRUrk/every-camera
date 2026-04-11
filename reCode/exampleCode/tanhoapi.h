#ifndef TANHOAPI_H
#define TANHOAPI_H

#include "tanhoapi_global.h"

class TANHOAPISHARED_EXPORT TanhoAPI
{

public:
    TanhoAPI();

    static  int TanhoCam_OpenDriver();
    static  int TanhoCam_CloseDriver();
    static  bool TanhoCam_DriverInit(unsigned int CAMERA_TYPE);
    static  void TanhoCam_GetFrameData(unsigned char* output);
    static  int  TanhoCam_ExecuteCmd(unsigned char* data);
};




#endif // TANHOAPI_H
