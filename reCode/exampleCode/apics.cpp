#include "apics.h"
#include "ui_apics.h"
#include<../TanhoAPI//tanhoapi.h>
#include<newT.h>

struct libusb_device_handle *devh;
int transferred_bytes=0;
APICS::APICS(QWidget *parent) :
    QMainWindow(parent),
    ui(new Ui::APICS)
{
    ui->setupUi(this);

}

APICS::~APICS()
{
    delete ui;
}
void APICS::handleImageReady(QImage image) {
    // 将图像显示到label
    ui->label->setPixmap(QPixmap::fromImage(image).scaled(640, 512));
    ui->label->setMinimumSize(700, 700);
}

newT *m_thread= nullptr;
void APICS::on_pushButton_clicked()
{
    if(!m_thread)
    {

        m_thread = new newT;
        connect(m_thread, &newT::imageReady, this, &APICS::handleImageReady);
        connect(this, &APICS::sendDataSignal,m_thread, &newT::handleData);
        m_thread->start();
    }


}

void APICS::on_pushButton_2_clicked()
{
    float iExpTimeUs=ui->doubleSpinBox->value();
    unsigned int UValue = iExpTimeUs * 20.0;
    unsigned char *buff = (unsigned char*)(char*)(&UValue);
    unsigned char data[32]={};
    data[0] = 0x00;
    data[1] = 0x06;
    data[2] = 0x00;
    data[3] = 0xFF;
    data[4] = buff[1];
    data[5] = buff[0];
    data[6] = buff[3];
    data[7] = buff[2];
    data[30] = 0x00;
    data[31] = 0x15;
    emit sendDataSignal(data);
}


void APICS::on_horizontalSlider_valueChanged(int value)
{
    ui->spinBox->setValue(ui->horizontalSlider->value());
    unsigned char data[32]={};
    unsigned int UValue = ui->spinBox->value();;
    if (UValue == 0)
    {
        data[0]= 0x0;
        data[1] = 0x06;
        data[2] = 0x00;
        data[3] = 0xFC;
        emit sendDataSignal(data);
    }
    else if (UValue == 1)
    {
        data[0] = 0x00;
        data[1] = 0x06;
        data[2] = 0x00;
        data[3] = 0xF6;
        emit sendDataSignal(data);
    }
    else
    {
        data[0] = 0x00;
        data[1] = 0x06;
        data[2] = 0x00;
        data[3] = 0xFd;
        emit sendDataSignal(data);

    }
}

void APICS::on_spinBox_valueChanged(int arg1)
{
    ui->horizontalSlider->setValue(ui->spinBox->value());
    unsigned char data[32]={};
    unsigned int UValue = ui->spinBox->value();
    if (UValue == 0)
    {
        data[0]= 0x0;
        data[1] = 0x06;
        data[2] = 0x00;
        data[3] = 0xFC;
        emit sendDataSignal(data);
    }
    else if (UValue == 1)
    {
        data[0] = 0x00;
        data[1] = 0x06;
        data[2] = 0x00;
        data[3] = 0xF6;
        emit sendDataSignal(data);
    }
    else
    {
        data[0] = 0x00;
        data[1] = 0x06;
        data[2] = 0x00;
        data[3] = 0xFd;
        emit sendDataSignal(data);

    }
}
