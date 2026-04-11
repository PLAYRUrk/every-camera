#ifndef APICS_H
#define APICS_H

#include <QMainWindow>

namespace Ui {
class APICS;
}

class APICS : public QMainWindow
{
    Q_OBJECT

public:
    explicit APICS(QWidget *parent = 0);
    ~APICS();
signals:
    void sendDataSignal(unsigned char* data);

private slots:
    void on_pushButton_clicked();
    void handleImageReady(QImage image);
    void on_pushButton_2_clicked();

    void on_horizontalSlider_valueChanged(int value);

    void on_spinBox_valueChanged(int arg1);

private:
    Ui::APICS *ui;

};

#endif // APICS_H
