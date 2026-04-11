#ifndef NEWTHREAD_H
#define NEWTHREAD_H
#include<QThread>
#include<QImage>
class newT: public QThread
{
     Q_OBJECT
public:
    newT(QObject *parent = nullptr);
protected:
    void run();
signals:
    //自定义信号，传递数据
    void imageReady(QImage image);
public slots:
    void handleData(unsigned char data[32]);
};

#endif // NEWTHREAD_H
