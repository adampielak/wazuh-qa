import os
import hashlib
import zlib
import socket
import sys
import threading
import struct
from wazuh_testing.tools import WAZUH_PATH
from Crypto.Cipher import AES, Blowfish
from Crypto.Util.Padding import pad, unpad
from struct import pack

class Cipher:
  def __init__(self,data,key):
    self.block_size = 16
    self.data = data
    self.key_blowfish = key
    self.key_aes = key[:32]

  def encrypt_aes(self):
    iv = b'FEDCBA0987654321'
    cipher = AES.new(self.key_aes,AES.MODE_CBC,iv)
    crp = cipher.encrypt(pad(self.data, self.block_size))
    return (crp)

  def decrypt_aes(self):
    iv = b'FEDCBA0987654321'
    cipher = AES.new(self.key_aes,AES.MODE_CBC,iv)
    dcrp = cipher.decrypt(pad(self.data, self.block_size))
    return (dcrp)

  def encrypt_blowfish(self):
    iv = b'\xfe\xdc\xba\x98\x76\x54\x32\x10'
    cipher = Blowfish.new(self.key_blowfish, Blowfish.MODE_CBC, iv)
    crp = cipher.encrypt(self.data)
    return (crp)

  def decrypt_blowfish(self):
    iv = b'\xfe\xdc\xba\x98\x76\x54\x32\x10'
    cipher = Blowfish.new(self.key_blowfish, Blowfish.MODE_CBC, iv)
    dcrp = cipher.decrypt(self.data)
    return (dcrp)

class RemotedSimulator:
    
    def __init__(self, server_address='127.0.0.1', remoted_port=1514, protocol='udp', mode='REJECT'):  
        self.protocol = protocol
        self.global_count = 1234567891
        self.local_count = 5555
        self.keys = ({},{}) 
        self.encryption_key = ""  
        self.mode = mode 
        self.server_address = server_address
        self.remoted_port = remoted_port
        self.listener_thread = threading.Thread(target=self.listener)
        self.listener_thread.setName('listener_thread')
        self.last_message_ctx = ""
        self.running = False
        self.start()

    def start(self):   
        if self.protocol == "tcp":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)   
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.server_address,self.remoted_port))
            self.sock.listen(1) 
        elif self.protocol == "udp":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.server_address,self.remoted_port))  
        
        self.running = True  
        self.listener_thread.start() 
        

    def stop(self):
        self.running = False 
        self.listener_thread.join()    
        self.sock.close() 


    # Generate encryption key (using agent metadata and key)   
    def create_encryption_key(self,id,name,key):
        sum1 = (hashlib.md5((hashlib.md5(name.encode()).hexdigest().encode() + hashlib.md5(id.encode()).hexdigest().encode())).hexdigest().encode())[:15]
        sum2 = hashlib.md5(key.encode()).hexdigest().encode()
        self.encryption_key = sum2 + sum1
        
    # Compose event from raw message
    def compose_sec_message(self, message):
        message = message.encode()
        random_number = b'55555'
        split = b':'
        global_counter = str(self.global_count).encode()
        local_counter = str(self.local_count).encode()

        msg = random_number + global_counter + split + local_counter + split + message
        msg_md5 = hashlib.md5(msg).hexdigest()
        sec_message = msg_md5.encode() + msg
        return (sec_message)

    # Add the Wazuh custom padding to each sec_message sent
    def wazuh_padding(self, compressed_sec_message):
        padding = 8
        extra = len(compressed_sec_message) % padding
        if extra > 0:
            padded_sec_message = (b'!' * (padding - extra)) + compressed_sec_message
        else:
            padded_sec_message = (b'!' * (padding)) + compressed_sec_message
        return (padded_sec_message)

    # Encrypt sec_message AES or Blowfish
    def encrypt(self, padded_sec_message, crypto_method):
        if crypto_method == "aes":
            encrypted_sec_message = Cipher(padded_sec_message,self.encryption_key).encrypt_aes()
        elif crypto_method == "blowfish":
            encrypted_sec_message = Cipher(padded_sec_message,self.encryption_key).encrypt_blowfish()
        return (encrypted_sec_message)
    
    # Add sec_message headers for AES or Blowfish Cyphers
    def headers(self, encrypted_sec_message, crypto_method):
        if crypto_method == "aes":            
            header = "#AES:".encode()
        elif crypto_method == "blowfish":            
            header = ":".encode()
        headers_sec_message = header + encrypted_sec_message
        return (headers_sec_message)

    #Create a sec_message to Agent
    def create_sec_message(self, message, crypto_method):
        # Compose sec_message
        sec_message = self.compose_sec_message(message)
        # Compress
        compressed_sec_message = zlib.compress(sec_message)
        # Padding
        padded_sec_message = self.wazuh_padding(compressed_sec_message)
        # Encrypt
        encrypted_sec_message = self.encrypt(padded_sec_message, crypto_method)
        # Add headers
        headers_sec_message = self.headers(encrypted_sec_message, crypto_method)
        return (headers_sec_message) 

    def createACK(self, crypto_method):        
        return self.create_sec_message("#!-agent ack ", crypto_method) 

    def createINVALID(self):        
        return  "INVALID".encode()

    def update_counters(self):
        if self.local_count >= 9997 :
            self.local_count = 0
            self.global_count = self.global_count + 1

        self.local_count = self.local_count +1

    def decrypt_message(self, data, crypto_method):          
        if crypto_method == 'aes':
            msg_removeheader = bytes(data[5:])
            msg_decrypted = Cipher(msg_removeheader,self.encryption_key).decrypt_aes()
        else:
            msg_removeheader = bytes(data[1:])
            msg_decrypted = Cipher(msg_removeheader,self.encryption_key).decrypt_blowfish()

        padding = 0
        while(msg_decrypted):
            if msg_decrypted[padding] == 33:
                padding += 1
            else:
                break
        msg_removepadding = msg_decrypted[padding:]
        msg_decompress = zlib.decompress(msg_removepadding)
        msg_decoded = msg_decompress.decode()
        
        return msg_decoded
        
    def listener(self):    
        while self.running:   
            if self.protocol == 'tcp': 
                # Wait for a connection          
                connection, client_address = self.sock.accept()                
                while self.running:                    
                    rcv = connection.recv(65536) 
                    if len(rcv) >= 4:
                        data = rcv[4:]  
                        data_len = ((rcv[3]&0xFF) << 24) | ((rcv[2]&0xFF) << 16) | ((rcv[1]&0xFF) << 8) | (rcv[0]&0xFF)
                        if data_len == len(data):                            
                            ret = self.process_message(client_address, data)
                            # Response -1 means connection have to be closed
                            if ret == -1:
                                connection.close()
                                break
                            # If there is a response, answer it
                            elif ret:
                                self.send(connection, ret)
                    else:
                        pass

            elif self.protocol == 'udp':                 
                data, client_address = self.sock.recvfrom(65536)                
                ret = self.process_message(client_address, data)
                # If there is a response, answer it
                if ret != None and ret != -1:
                    self.send(client_address, ret)

    def send(self, dst, data):
        self.update_counters()
        if self.protocol == "tcp":
            length = pack('<I', len(data))
            dst.send(length+data)
        elif self.protocol == "udp":
            self.sock.sendto(data, dst)   
    
    def process_message(self, source, received):
        #parse agent identifier and payload
        index = received.find(b'!')        
        if index == 0:
            agent_identifier_type = "by_id"
            index = received[1:].find(b'!')
            agent_identifier = received[1:index+1].decode()
            received=received[index+2:]
        else:
            agent_identifier_type = "by_ip"
            agent_identifier = source[0]

        #parse crypto method
        if received.find(b'#AES') == 0:
            crypto_method = "aes"
        else:
            crypto_method = "blowfish"

        #Update keys to encrypt/decrypt        
        self.update_keys()
        (id, name, ip, key) = self.get_key()  
        self.create_encryption_key(id, name, key) 

        #Decrypt message
        rcv_msg = self.decrypt_message(received, crypto_method) 

        #Hash message means a response is required   
        if rcv_msg.find('#!-') != -1:
            hash_message = True
        else:
            hash_message = False

        #Save context of received message for future asserts
        self.last_message_ctx = '{} {} {}'.format(agent_identifier_type, agent_identifier, crypto_method)
        
        #Create response
        if self.mode == "REJECT":
            return -1
        elif self.mode == "DUMMY_ACK":                       
            msg = self.createACK(crypto_method) 
        elif self.mode == "CONTROLED_ACK":            
            if hash_message :
                msg = self.createACK(crypto_method) 
            else:
                msg = None
        elif self.mode == "WRONG_KEY":
            self.create_encryption_key(id+'inv', name+'inv', key+'inv')      
            msg = self.createACK(crypto_method) 
        elif self.mode == "INVALID_MSG":
            msg = self.createINVALID()      

        return(msg)

    def update_keys(self):
        client_keys_path = os.path.join(WAZUH_PATH, 'etc', 'client.keys')

        with open(client_keys_path) as client_file:
            client_lines = client_file.read().splitlines() 

            self.keys = ({},{}) 
            for line in client_lines:
                (id, name, ip, key) = line.split(" ")
                self.keys[0][id] = (id, name, ip, key)
                self.keys[1][ip] = (id, name, ip, key)
    
    def get_key(self, key=None, dictionary="by_id"):
        if key==None:            
            return next(iter(self.keys[0].values()))

        if dictionary == "by_ip":
            return self.keys[0][key]
        else:
            return self.keys[1][key]

    def set_mode(self, mode):
        self.mode = mode
