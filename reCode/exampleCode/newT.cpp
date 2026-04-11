#include "newT.h"
#include<../TanhoAPI//tanhoapi.h>
#include<QThread>
#include<QImage>
newT::newT(QObject *parent):QThread(parent)
{

}
unsigned char datanew[32]={};
unsigned char dataold[32]={};
void newT:: handleData(unsigned char data[32])
{
     memcpy(datanew,data,32);
}

void newT::run()
{
    TanhoAPI api;
   api.TanhoCam_DriverInit(1);
   int isopen=api.TanhoCam_OpenDriver();
   unsigned char* data=(unsigned char*)malloc(921600);
   unsigned char  dst[640*512]={};
 while (isopen)
 {

     if(memcmp(dataold, datanew, 32) != 0 && datanew !=NULL)
     {
         api.TanhoCam_ExecuteCmd(datanew);
         memcpy(dataold, datanew, 32);
     }
     api. TanhoCam_GetFrameData(data);
     for (int j = 1; j <640*512; j++)
     {
         int byte_index = j * 2+1;
         dst[j] =data[byte_index];
     }
     QImage image=QImage(dst,640,512,640,QImage::Format_Grayscale8).copy();

    emit imageReady(image);
 }

}
