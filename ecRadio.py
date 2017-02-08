from gnuradio import gr, gru, modulation_utils
from gnuradio import usrp
from gnuradio import eng_notation
from gnuradio.eng_option import eng_option
from optparse import OptionParser

import gobject
import threading
import random, time
import struct
import gtk
import sys
import os
import string
import array
import cPickle

from transmit_path import transmit_path
from receive_path import receive_path
import fusb_options

class ecRadioUSRPConnection(gr.top_block,threading.Thread):
    def __init__(self, mod_class, demod_class,
                 rx_callback, options):
        threading.Thread.__init__(self)
        gr.top_block.__init__(self)
        self.txpath = transmit_path(mod_class, options)
        self.rxpath = receive_path(demod_class, rx_callback, options)
        
	self.connect(self.txpath);
	self.connect(self.rxpath);
	
    def send_pkt(self, payload='', eof=False):
        return self.txpath.send_pkt(payload, eof)

    def carrier_sensed(self):
        return self.rxpath.carrier_sensed()

class ecRadioEngine(threading.Thread):
	def __init__ (self):
		threading.Thread.__init__(self)
		self.usrp	= None
		self.pipelinePool = []
		self.engineID = int(random.random() * 100)
		self.engineIdleFlag = threading.Event()
		self.engineIdleFlag.set()
		self.packetSize = 1024 # in bytes
		self.waitTime = 0.01 # in seconds
		self.DATA = 0x01
		self.DATA_START = 0x02
		self.DATA_FINISH = 0x03
		self.DATA_BUSY = 0x04
		self.DATA_AGAIN = 0x05
		self.DATA_READYTOSTART = 0x06
		self.DATA_READYTOFINISH = 0x07
		#Test variables
		self.txTotal = 0
		self.rxTotal = 0
		self.startTime = 0
		self.finishTime = 0
		self.crcError = 0

		#print "ecEngine" + str(self.engineID) +" created..."
		
	def makeConnection(self,connection):
		self.usrp = connection	

	def addPipeline(self,pipeline):
		self.pipelinePool.append(pipeline)
		#print "Engine"+str(self.engineID) + ":Pipeline" + str(pipeline.pipelineID) + " added..."

	def encapsulateData(self, pipelineID, pipelineAcknowledge, packetNumber, packetTotal, data):
		dataArray = [pipelineID,pipelineAcknowledge,packetNumber,packetTotal,data]
		encapsulatedData = cPickle.dumps(dataArray)
		return encapsulatedData

	def decapsulateData(self, encapsulatedData):
		try:
			return cPickle.loads(encapsulatedData)
		except Exception,error:
			#print "Engine"+str(self.engineID) + ": decapsulate error..."			
			return None

	def onPayloadReceived(self, ok, usrpPayload):
		#parsedMsg[0] = pipeline ID
		#parsedMsg[1] = packet acknowledge
		#parsedMsg[2] = packet no
		#parsedMsg[3] = packet total
		#parsedMsg[4] = data
		#cekilis = int(random.random() * 100)
		self.rxTotal += 1
		self.engineIdleFlag.clear()
		if ok:
			parsedMsg = self.decapsulateData(usrpPayload)
			if parsedMsg:
				for pipe in self.pipelinePool: 
					if parsedMsg[0] == pipe.pipelineID:
						#Receiving request to start data
						if parsedMsg[1] == self.DATA_START:
							#print "Engine"+str(self.engineID) + ": DATA RX [START REQUEST]"
							#Lets check if our receiver is busy or not
							if pipe.rxIdleFlag.isSet() or pipe.rxPacketNo == 0:
								#Our receiver is idle so lets inform that we are ready and prepare rx buffer
								#Test variables
								self.txTotal = 0
								self.rxTotal = 0
								self.startTime = 0
								self.finishTime = 0
								self.crcError = 0
								self.startTime = time.time()
								pipe.rxError = []
								pipe.rxBuffer = []
								pipe.rxError.extend(range(0,parsedMsg[3]))
								pipe.rxBuffer.extend(range(0,parsedMsg[3]))
								for n in range(0,parsedMsg[3]):
									pipe.rxBuffer[n] = None
								pipe.rxPacketNo = 0
								pipe.rxPacketTotal = parsedMsg[3]
								response = self.encapsulateData(pipe.pipelineID,
																self.DATA_READYTOSTART,
																0,
																pipe.rxPacketTotal,
																None)
								self.actionSendPayload(response)
								pipe.rxIdleFlag.clear()						
							else:
								#Our receiver is busy so lets inform that we are busy
								response = self.encapsulateData(pipe.pipelineID,
																self.DATA_BUSY,
																0,
																pipe.rxPacketTotal,
																None)
								self.actionSendPayload(response)

						#Receiving data packet									
						elif parsedMsg[1] == self.DATA:
							#print "Engine"+str(self.engineID)+": DATA RX:"+str(parsedMsg[2])+"/"+str(parsedMsg[3])
							if pipe.rxPacketTotal == parsedMsg[3]:
								#print str(parsedMsg[2]) + "/" + str(parsedMsg[3])
								pipe.rxBuffer[parsedMsg[2]-1] = parsedMsg[4]
								pipe.rxError[parsedMsg[2]-1] = None
								pipe.rxPacketNo += 1
						#Receiving request to finish data
						elif parsedMsg[1] == self.DATA_FINISH:
							if pipe.rxPacketTotal == parsedMsg[3]:
								#print "Engine"+str(self.engineID) + ": DATA RX [FINISH REQUEST]"
								#Calculating missed packets
								pipe.rxErrorFound = []
								for n in range(0,pipe.rxPacketTotal):
									if pipe.rxError[n]:
										pipe.rxErrorFound.append(pipe.rxError[n] + 1)
								if pipe.rxErrorFound:
									error = self.encapsulateData(pipe.pipelineID,
																self.DATA_AGAIN,
																pipe.rxPacketNo,
																pipe.rxPacketTotal,
																pipe.rxErrorFound[0:10])
									self.actionSendPayload(error)
									pipe.rxErrorFound = pipe.rxErrorFound[10:]
								else:
									accepted = self.encapsulateData(pipe.pipelineID,
																self.DATA_READYTOFINISH,
																pipe.rxPacketNo,
																pipe.rxPacketTotal,
																None)
									self.actionSendPayload(accepted)
									self.finishTime = time.time()
									pipe.rxIdleFlag.set()
									print "------------------------------------------"
									print "FILE TRANSFER FINISHED"
									print "Total TxPacket: "+str(self.txTotal)
									print "Total RxPacket: "+str(self.rxTotal)
									print "Total RxCRC Error: "+str(self.crcError)
									print "Elapsed Time: "+str(self.finishTime - self.startTime)+" secs"
									if pipe.txBuffer:
										print "File Transfer Speed TX: " + str(len(pipe.txBuffer)/((self.finishTime - self.startTime))) + " KByte/sec"
									if pipe.rxBuffer:
										print "File Transfer Speed RX: " + str(len(pipe.rxBuffer)/((self.finishTime - self.startTime))) + " KByte/sec"
							else:
								#print "THERE ARE ERRORS SO WE WILL REQUEST AGAIN"
								#print "Engine"+str(self.engineID) + ": DATA RX [ERROR]"
								again = self.encapsulateData(pipe.pipelineID,
															self.DATA_AGAIN,
															(pipe.rxPacketNo),
															pipe.rxPacketTotal,
															None)
								self.actionSendPayload(again)
						#Receiving request to send data again
						elif parsedMsg[1] == self.DATA_AGAIN:
							#print "Engine"+str(self.engineID) + ": DATA RX [ERROR ACCEPTED]" + str(parsedMsg[2])
							if pipe.txPacketTotal == parsedMsg[3]:
								pipe.txError = parsedMsg[4]
								#sys.exit(0)
						#Receiving status of receiver that is ready
						elif parsedMsg[1] == self.DATA_READYTOSTART:
							#print "Engine"+str(self.engineID) + ": DATA RX [START ACCEPTED]"
							pipe.txPacketNo = 1
							pipe.txIdleFlag.clear() 
						elif parsedMsg[1] == self.DATA_READYTOFINISH:
							print "Engine"+str(self.engineID) + ": DATA RX [FINISH ACCEPTED]"
							pipe.txIdleFlag.set()
							self.finishTime = time.time()
							print "------------------------------------------"
							print "FILE TRANSFER FINISHED"
							print "Total TxPacket: "+str(self.txTotal)
							print "Total RxPacket: "+str(self.rxTotal)
							print "Total RxCRC Error: "+str(self.crcError)
							print "Elapsed Time: "+str(self.finishTime - self.startTime)+" secs"
							if pipe.txBuffer:
								print "File Transfer Speed TX: " + str(len(pipe.txBuffer)/((self.finishTime - self.startTime))) + " KByte/sec"
							if pipe.rxBuffer:
								print "File Transfer Speed RX: " + str(len(pipe.rxBuffer)/((self.finishTime - self.startTime))) + " KByte/sec"
						#Receiving status of receiver that is busy	
						elif parsedMsg[1] == self.DATA_BUSY:
							print "Waiting"		
						else:
							pass
							#print "Engine"+str(self.engineID) + ": Unhandled Protocol Acknowledge Flag"		
			else:
				#print "Engine"+str(self.engineID) + ": Decapsulate ERROR"
				pass
		else:
			#print "Engine"+str(self.engineID) + ": CRC ERROR"
			self.crcError += 1
			pass
		self.engineIdleFlag.set()
	def actionSendPayload(self, payload=None):
		#self.readyToSend.set()
		self.txTotal += 1
		self.usrp.send_pkt(payload)		
	def run(self):
		while 1:
			for pipe in self.pipelinePool:
				self.engineIdleFlag.wait()
				if not pipe.txIdleFlag.isSet():
					#Transmits errored packets again
					if pipe.txError:
						#print "Engine"+str(self.engineID)+": DATA TX AGAIN:"+str(pipe.txError[0])+"/"+str(pipe.txPacketTotal)
						dataNext = self.encapsulateData(pipe.pipelineID
													,self.DATA
													,pipe.txError[0]
													,pipe.txPacketTotal
													,pipe.txBuffer[pipe.txError[0]-1]);
						self.actionSendPayload(dataNext);
						del pipe.txError[0]	
						#time.sleep(1)
					#Transmits and manages new data session
					elif pipe.txPacketNo == 0:
						print "Engine"+str(self.engineID)+": DATA TX [START REQUEST]"
						#Test variables
						self.txTotal = 0
						self.rxTotal = 0
						self.startTime = 0
						self.finishTime = 0
						self.crcError = 0
						self.startTime = time.time()					
						dataStart = self.encapsulateData(pipe.pipelineID
													,self.DATA_START
													,pipe.txPacketNo
													,pipe.txPacketTotal
													,None);
						self.actionSendPayload(dataStart)
						time.sleep(0.1)
					elif pipe.txPacketNo <= pipe.txPacketTotal:
						#print "Engine"+str(self.engineID)+": DATA TX:"+str(pipe.txPacketNo)+"/"+str(pipe.txPacketTotal)	
						dataNext = self.encapsulateData(pipe.pipelineID
													,self.DATA
													,pipe.txPacketNo
													,pipe.txPacketTotal
													,pipe.txBuffer[pipe.txPacketNo - 1]);
						self.actionSendPayload(dataNext);
						pipe.txPacketNo += 1	
					elif pipe.txPacketNo > pipe.txPacketTotal:	
						#print "Engine"+str(self.engineID)+": DATA TX [FINISH REQUEST]"			
						dataFinish = self.encapsulateData(pipe.pipelineID
													,self.DATA_FINISH
													,pipe.txPacketNo
													,pipe.txPacketTotal
													,None);
						self.actionSendPayload(dataFinish)
						time.sleep(0.1)		
				else:
					time.sleep(0.1)	
					pass				

	def stop(self):
		print "Engine"+str(self.engineID)+" stopped.."
		self = None
	

class ecRadioPipeline(threading.Thread):
	def __init__(self, pipelineId=None):
		threading.Thread.__init__(self)
		if not pipelineId:
			self.pipelineID = int(random.random() * 100000000);
		else:
			self.pipelineID = pipelineId
		
		self.txIdleFlag = threading.Event()
		self.rxIdleFlag = threading.Event()
		self.txBuffer = []
		self.rxBuffer = []
		self.txError = []
		self.rxError = []
		self.rxErrorFound = []
		
		self.txPacketNo = 0
		self.rxPacketNo = 0	
		self.txPacketTotal = 0
		self.rxPacketTotal = 0
		
		self.txIdleFlag.set()
		self.rxIdleFlag.set()
		
		self.packetSize = 1024
		
		print "Pipeline"+str(self.pipelineID)+" created..."

	def run(self):
		pass
		
	def sendData(self,txdata):
		#self.txIdleFlag.wait()
		#Checking pipeline tx buffer is busy or not
		if self.txIdleFlag.isSet():
			#Calculates total packet number
			totalPacket = int(len(txdata) / self.packetSize) + 1
			
			#Clears tx buffers
			self.txError = []
			self.txBuffer = []
			
			#Splits data
			self.txBuffer.extend(range(0,totalPacket))
			for n in range(0,totalPacket):
				self.txBuffer[n] = txdata[n*self.packetSize:n*self.packetSize + self.packetSize]
			
			#Tx buffer information
			self.txPacketNo = 0
			self.txPacketTotal = totalPacket
			#Makes pipeline tx status to busy
			self.txIdleFlag.clear()
		else:
			print "Pipeline"+str(self.pipelineID)+" TX busy..."
		
	def getData(self):
		self.rxIdleFlag.wait()
		returnData = ""
		if self.rxPacketTotal != 0:
			#Rebuilds data
			for n in range(0,self.rxPacketTotal):
				if n == 0:
					returnData = self.rxBuffer[0]
				else:
					returnData += self.rxBuffer[n]
			self.rxError = []
			self.rxErrorFound = []
			self.rxBuffer = []
			self.rxPacketNo = 0
			self.rxPacketTotal = 0
			return returnData
		else:
			return None
		
	def stop(self):
		print "Pipeline"+str(self.pipelineID)+" stopped.."
		self = None

class ecRadioForm:
	def __init__(self):
		gtk.gdk.threads_init()
		mods = modulation_utils.type_1_mods()
		demods = modulation_utils.type_1_demods()

		parser = OptionParser (option_class=eng_option, conflict_handler="resolve")
		expert_grp = parser.add_option_group("Expert")

		parser.add_option("-m", "--modulation", type="choice", choices=mods.keys(),
				          default='gmsk',
				          help="Select modulation from: %s [default=%%default]"
				                % (', '.join(mods.keys()),))

		parser.add_option("-v","--verbose", action="store_true", default=False)
		expert_grp.add_option("-c", "--carrier-threshold", type="eng_float", default=30,
				              help="set carrier detect threshold (dB) [default=%default]")
		expert_grp.add_option("","--tun-device-filename", default="/dev/net/tun",
				              help="path to tun device file [default=%default]")

		transmit_path.add_options(parser, expert_grp)
		receive_path.add_options(parser, expert_grp)

		for mod in mods.values():
			mod.add_options(expert_grp)

		for demod in demods.values():
			demod.add_options(expert_grp)

		fusb_options.add_options(expert_grp)

		(options, args) = parser.parse_args ()
		if len(args) != 0:
			parser.print_help(sys.stderr)
			sys.exit(1)

		if options.rx_freq is None or options.tx_freq is None:
			sys.stderr.write("You must specify -f FREQ or --freq FREQ\n")
			parser.print_help(sys.stderr)
			sys.exit(1)
		# Attempt to enable realtime scheduling
		r = gr.enable_realtime_scheduling()
		if r == gr.RT_OK:
			realtime = True
		else:
			realtime = False
			print "Note: failed to enable realtime scheduling"


		# If the user hasn't set the fusb_* parameters on the command line,
		# pick some values that will reduce latency.

		if options.fusb_block_size == 0 and options.fusb_nblocks == 0:
			if realtime:                        # be more aggressive
				options.fusb_block_size = gr.prefs().get_long('fusb', 'rt_block_size', 1024)
				options.fusb_nblocks    = gr.prefs().get_long('fusb', 'rt_nblocks', 16)
			else:
				options.fusb_block_size = gr.prefs().get_long('fusb', 'block_size', 4096)
				options.fusb_nblocks    = gr.prefs().get_long('fusb', 'nblocks', 16)

		#print "fusb_block_size =", options.fusb_block_size
		#print "fusb_nblocks    =", options.fusb_nblocks

		# INITIALIZE COMPONENTS
		self.engine1 = ecRadioEngine()
		self.pipe1 = ecRadioPipeline(1)
		self.usrp1 = ecRadioUSRPConnection(mods[options.modulation],
				          demods[options.modulation],
				          self.engine1.onPayloadReceived,
				          options)

		self.engine1.makeConnection(self.usrp1)
		self.engine1.addPipeline(self.pipe1)

		if self.usrp1.txpath.bitrate() != self.usrp1.rxpath.bitrate():
			print "WARNING: Transmit bitrate = %sb/sec, Receive bitrate = %sb/sec" % (
				eng_notation.num_to_str(self.usrp1.txpath.bitrate()),
				eng_notation.num_to_str(self.usrp1.rxpath.bitrate()))
				 
		print "modulation:     %s"   % (options.modulation,)
		print "freq:           %s"      % (eng_notation.num_to_str(options.tx_freq))
		print "bitrate:        %sb/sec" % (eng_notation.num_to_str(self.usrp1.txpath.bitrate()),)
		print "samples/symbol: %3d" % (self.usrp1.txpath.samples_per_symbol(),)
		#print "interp:         %3d" % (usrp.txpath.interp(),)
		#print "decim:          %3d" % (usrp.rxpath.decim(),)

		self.usrp1.rxpath.set_carrier_threshold(options.carrier_threshold)
		print "Carrier sense threshold:", options.carrier_threshold, "dB"
		self.engine1.start()
		self.pipe1.start()
		self.usrp1.start()
 
	def actionQuitProgram(self, obj):
		self.engine1.stop()
		self.pipe1.stop()
		self.usrp1.stop()
		#self.usrp1.wait()
		gtk.main_quit()
		sys.exit()
 
	def action1SendData(self, button, data=None):
		print "Testing 10kb..."
		fileobj = open("test1.pdf", mode='rb')
		dosyaVerisi = fileobj.read()
		fileobj.close()
		#veriArray = ["djshadow.mp3", dosyaVerisi]
		#seriVeri = cPickle.dumps(veriArray)
		self.pipe1.sendData(dosyaVerisi)
		self.pipe1.txIdleFlag.wait()
		self.pipe1.sendData(dosyaVerisi)
		self.pipe1.txIdleFlag.wait()
		self.pipe1.sendData(dosyaVerisi)
		self.pipe1.txIdleFlag.wait()
		
		print "Testing 100kb..."
		fileobj = open("test2.pdf", mode='rb')
		dosyaVerisi = fileobj.read()
		fileobj.close()
		#veriArray = ["djshadow.mp3", dosyaVerisi]
		#seriVeri = cPickle.dumps(veriArray)
		self.pipe1.sendData(dosyaVerisi)
		self.pipe1.txIdleFlag.wait()
		self.pipe1.sendData(dosyaVerisi)
		self.pipe1.txIdleFlag.wait()
		self.pipe1.sendData(dosyaVerisi)
		self.pipe1.txIdleFlag.wait()
		
		print "Testing 1200kb..."
		fileobj = open("test3.pdf", mode='rb')
		dosyaVerisi = fileobj.read()
		fileobj.close()
		#veriArray = ["djshadow.mp3", dosyaVerisi]
		#seriVeri = cPickle.dumps(veriArray)
		self.pipe1.sendData(dosyaVerisi)
		self.pipe1.txIdleFlag.wait()
		self.pipe1.sendData(dosyaVerisi)
		self.pipe1.txIdleFlag.wait()
		self.pipe1.sendData(dosyaVerisi)
		self.pipe1.txIdleFlag.wait()
		#self.pipe1.sendData(seriVeri)
	def action2SendData(self, button, data=None):
		dosyaGelen = self.pipe1.getData()
		fileobj2 = open("_"+self.entry.get_text(), mode='wb')
		fileobj2.write(dosyaGelen)
		fileobj2.close()
	def main(self):
		window = gtk.Window()
		vbox = gtk.VBox()
		self.entry = gtk.Entry()
		button1 = gtk.Button("SEND DATA ->" + str(self.engine1.engineID))
		button2 = gtk.Button("SAVE DATA <-" + str(self.engine1.engineID))
		vbox.pack_start(self.entry, False, True)
		vbox.pack_start(button1, True, True)
		vbox.pack_start(button2, True, True)
		window.add(vbox)
		window.show_all()
		button1.connect("clicked", self.action1SendData)
		button2.connect("clicked", self.action2SendData)
		window.connect('destroy', self.actionQuitProgram)
 
		gtk.gdk.threads_enter()
		gtk.main()
		gtk.gdk.threads_leave()
 
if __name__ == '__main__':
	ec = ecRadioForm()
	ec.main()
